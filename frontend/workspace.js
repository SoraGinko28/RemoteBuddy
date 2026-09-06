"use strict";

const LIVE_SCREEN_FRAME_MS = 700;
const MAX_LIVE_SCREEN_QUEUE = 120;
// Playback pacing for frames captured while the human owns the terminal.
// Takeover frames are recorded every ~0.2-0.35s, so pacing each at the
// generic 700ms makes a takeover stretch replay slower than it actually
// happened ("I did not type that slowly"). They are replayed fast-forward
// instead; idle gaps between takeover bursts produce no frames, so pauses
// still jump instantly.
const HUMAN_TAKEOVER_FRAME_MS = 200;

const el = Object.fromEntries([
  "health-dot", "health-message", "new-button", "welcome-new-button", "meeting-count", "meeting-list", "list-empty",
  "welcome", "session-view", "meeting-title", "agent-states", "status-badge", "owner-badge",
  "phase-label", "backend-label", "provider-label", "model-label", "typing-profile-label", "action-label", "runtime-label", "event-count", "failure-message", "refresh-button",
  "resume-button", "complete-button", "cleanup-button", "stop-button", "chat-list", "message-form",
  "message-input", "send-message-button", "message-hint", "delivery-mode", "reference-card", "composer-shell", "evaluation-panel", "evaluation-form", "evaluation-excerpt", "evaluation-comment", "evaluation-error", "evaluation-submit-button", "evaluation-back-button", "evaluation-cancel-button", "terminal-command", "terminal-path",
  "terminal-screen", "typing-state", "terminal-readonly", "control-label", "acquire-button", "release-button",
  "terminal-copy-button", "terminal-copy-ansi-button",
  "play-button",
  "playback-track", "playback-slider", "playback-position", "playback-hover", "live-button", "inspect-button", "free-scroll-button", "follow-scroll-button", "settings-button", "settings-dialog", "settings-form", "judge-name-input", "save-settings", "close-settings", "cancel-settings", "settings-error", "terminal-score-status", "selection-menu", "new-dialog", "new-form",
  "close-dialog", "cancel-new", "initial-message", "backend-select", "provider-select", "model-input", "cli-default-model-row", "cli-default-model", "api-credential-fields", "effort-select", "effort-live-select", "start-button", "new-error",
  "api-key-input", "base-url-input", "api-key-hint", "api-key-name",
  "event-dialog", "event-title", "event-content", "close-event", "toast",
].map((id) => [id.replaceAll("-", "_"), document.querySelector(`#${id}`)]));

const state = {
  csrfToken: "",
  csrfHeader: "X-RemoteBuddy-CSRF",
  healthy: false,
  meetings: [],
  selectedId: null,
  snapshot: null,
  timeline: [],
  cursor: 0,
  playbackIndex: -1,
  playbackFreezeAt: 0,
  followLive: true,
  viewportMode: "follow",
  chatFollow: true,
  renderingChat: false,
  // scrollTop value most recently applied by renderChat; used to tell a real
  // user scroll apart from the follow-scroll renderChat performs itself.
  chatScrollApplied: null,
  playing: false,
  timer: null,
  pollTimer: null,
  liveScreenTimer: null,
  liveScreenEvent: null,
  liveScreenQueue: [],
  chatFilter: "all",
  toastTimer: null,
  references: [],
  // Legacy alias kept for extensions that still read the old singular field.
  reference: null,
  highlightReference: null,
  pendingSelection: null,
  selectionBusy: false,
  selectionTimer: null,
  selectionMenuDelay: 0,
  keyboardSelection: null,
  evaluationSelection: null,
  evaluationBusy: false,
  scoreOverlayIds: new Set(),
  playbackHoverIndex: null,
  judgeName: "human",
  health: null,
  pointerSelecting: false,
};

const labels = {
  status: { created: "已创建", starting: "启动中", running: "协作中", completed: "已完成", failed: "失败", interrupted: "已中止" },
  phase: { created: "等待启动", starting: "正在打开终端", agent_thinking: "{agent} 思考中", agent_acting: "{agent} 操作中", waiting_for_user: "等待你的消息", completion_proposed: "{agent} 提议完成", human_control: "由你控制", api_error: "模型连接中断", cycle_timeout: "本轮已超时", completed: "已结束", failed: "执行失败", interrupted: "已中止" },
  owner: { agent: "{agent} 控制", human: "你在控制", none: "无人控制" },
  message: { request: "请求", ack: "回应", progress: "进度", question: "问题", final: "结果" },
  typing: { demo: "演示", balanced: "平衡", fast: "快速" },
  score: { competent: "胜任", incompetent: "不胜任", unscored: "无评分" },
  backend: { responses: "Responses API", "codex-app-server": "Codex App Server", "claude-code": "Claude Code" },
  provider: { openai: "OpenAI", deepseek: "DeepSeek" },
  agentState: { thinking: "思考中", acting: "操作中", idle: "待命中", error: "异常" },
  effort: { none: "无", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高" },
};

// Ordinals are reserved for future multi-agent sessions; today every
// conversation has one agent and it is always "Agent1".
function agentName(meeting) {
  const n = Number(meeting?.agent_ordinal);
  return `Agent${Number.isInteger(n) && n > 0 ? n : 1}`;
}

function withAgent(text, meeting) {
  return String(text).replaceAll("{agent}", agentName(meeting));
}

// Every phase outside these buckets (terminal states, human takeover,
// waiting) renders as the muted "待命中" badge; error details stay in
// #failure-message.
const AGENT_STATE_PHASES = {
  starting: "thinking",
  agent_thinking: "thinking",
  agent_acting: "acting",
  api_error: "error",
  cycle_timeout: "error",
  failed: "error",
};

function agentStateForPhase(phase) {
  return AGENT_STATE_PHASES[phase] || "idle";
}

// Per-member state for the header chips. The generator follows terminal
// ownership; every other member (discriminator/reviewer) is "active" while
// the newest thinking/acting band in the timeline belongs to it.
const IDLE_PHASE_BOUNDARY = new Set([
  "created", "waiting_for_user", "human_control", "completion_proposed", "cycle_timeout", "api_error",
]);

// If a terminal owner claims to be thinking/acting but authored no event for
// this long, the worker is likely orphaned (state parked after a crash);
// stop reporting a live turn.
const STALE_BAND_MS = 180_000;

function latestBandTime(actor) {
  const timeline = state.timeline || [];
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const event = timeline[index];
    const phase = event?.phase;
    if (event?.actor === actor) {
      if (phase === "agent_thinking" || phase === "agent_acting") {
        const stamp = Date.parse(event.captured_at || "");
        return Number.isFinite(stamp) ? stamp : 0;
      }
      if (IDLE_PHASE_BOUNDARY.has(phase)) return 0;
      continue;
    }
    if (IDLE_PHASE_BOUNDARY.has(phase)) return 0;
    if (event?.actor === "harness" || !phase) continue;
  }
  return 0;
}

function memberState(actor, meeting) {
  const terminal = ["completed", "failed", "interrupted"].includes(meeting.status);
  if (terminal) return "idle";
  if (meeting.control_owner === actor) {
    const stateName = agentStateForPhase(meeting.phase);
    if ((stateName === "thinking" || stateName === "acting")
        && Date.now() - latestBandTime(actor) > STALE_BAND_MS) {
      return "error";  // parked/orphaned: no live turn for minutes
    }
    return stateName;
  }
  // A non-owner member is busy only while nobody owns the terminal (reviewer
  // turns run with control_owner "none") AND the meeting is mid-turn AND the
  // newest thinking/acting band in the timeline belongs to that member.
  // Whenever the meeting has parked (waiting etc.) or a terminal owner holds
  // the turn, every non-owner member is idle.
  if (meeting.control_owner !== "none") return "idle";
  if (!["agent_thinking", "agent_acting"].includes(meeting.phase)) return "idle";
  const timeline = state.timeline || [];
  for (let index = timeline.length - 1; index >= 0; index -= 1) {
    const event = timeline[index];
    const phase = event?.phase;
    if (event?.actor === actor) {
      if (phase === "agent_thinking" || phase === "agent_acting") {
        return phase === "agent_acting" ? "acting" : "thinking";
      }
      if (IDLE_PHASE_BOUNDARY.has(phase)) return "idle";
      continue;
    }
    if (IDLE_PHASE_BOUNDARY.has(phase)) return "idle";
    if (event?.actor === "harness" || !phase) continue;
  }
  return "idle";
}

function renderMemberStates(meeting) {
  if (!el.agent_states) return;
  const roles = (meeting.agent_name2role && Object.keys(meeting.agent_name2role).length)
    ? meeting.agent_name2role
    : { "agent": "generator" };
  el.agent_states.replaceChildren();
  for (const actor of Object.keys(roles)) {
    const stateName = memberState(actor, meeting);
    const chip = node("span", `agent-state ${stateName}`);
    const dot = document.createElement("span");
    dot.className = "agent-state-dot";
    dot.setAttribute("aria-hidden", "true");
    chip.append(dot, document.createTextNode(
      `${actor} · ${labels.agentState[stateName] || "待命"}`));
    el.agent_states.append(chip);
  }
  el.agent_states.hidden = false;
}

function eventDelay(event, queueLength = 0) {
  if (event?.kind !== "screen") return 250;
  if (event.phase === "human_control") return HUMAN_TAKEOVER_FRAME_MS;
  const kind = event.data?.change_kind;
  if (!['typing', 'paste'].includes(kind)) return LIVE_SCREEN_FRAME_MS;
  const raw = Number(event.data?.planned_delay_ms);
  const lower = kind === "paste" ? 40 : 55;
  const upper = kind === "paste" ? 120 : 500;
  let delay = Number.isFinite(raw) ? Math.min(upper, Math.max(lower, raw)) : lower;
  if (queueLength > 24) delay *= Math.max(0.35, 24 / queueLength);
  return Math.round(delay);
}

function node(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = String(text);
  return value;
}

async function api(path, options = {}, retryCsrf = true) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (method !== "GET") headers.set(state.csrfHeader, state.csrfToken);
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (_error) { payload = { error: text }; }
  if (
    !response.ok
    && response.status === 403
    && method !== "GET"
    && retryCsrf
    && /csrf/i.test(String(payload.error || ""))
  ) {
    const sessionResponse = await fetch("/api/session", { cache: "no-store" });
    if (sessionResponse.ok) {
      const session = await sessionResponse.json();
      if (typeof session.csrf_token === "string" && session.csrf_token) {
        state.csrfToken = session.csrf_token;
        state.csrfHeader = session.csrf_header || state.csrfHeader;
        return api(path, options, false);
      }
    }
  }
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

async function apiConditional(path, etag) {
  const headers = new Headers();
  if (etag) headers.set("If-None-Match", etag);
  const response = await fetch(path, { headers, cache: "no-store" });
  if (response.status === 304) return { notModified: true, etag };
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (_error) { payload = { error: text }; }
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return { notModified: false, etag: response.headers.get("ETag") || "", payload };
}

function toast(message) {
  clearTimeout(state.toastTimer);
  el.toast.textContent = message;
  el.toast.hidden = false;
  state.toastTimer = setTimeout(() => { el.toast.hidden = true; }, 5000);
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function duration(value) {
  const start = new Date(value).getTime();
  if (!Number.isFinite(start)) return "00:00";
  const total = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const minutes = Math.floor(total / 60);
  return `${String(minutes).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function eventAt(index) {
  return index >= 0 && index < state.timeline.length ? state.timeline[index] : null;
}

// A human keystroke is journaled verbatim as ground truth: three bookkeeping
// actions per stroke (received/started/completed). During playback those
// events carry no visuals of their own -- the takeover terminal paints through
// the interleaved screen frames -- so auto-play coalesces them: a whole run is
// skipped in one step and rendering resumes at the next real event. Per-event
// pacing of these events only adds per-character stalls (250ms or even a 1ms
// timer still has to fire per recorded event), so a typed burst flashes to the
// next frame instead. Recording stays per-stroke.
function isHumanKeystrokeEvent(event) {
  return Boolean(
    event
    && event.kind === "action"
    && event.actor === "human"
    && event.phase === "human_control"
    && ["received", "started", "completed"].includes(event.data?.status),
  );
}

function playbackSequence() {
  if (state.followLive) return Number.POSITIVE_INFINITY;
  const current = eventAt(state.playbackIndex);
  if (!current) return Number.NEGATIVE_INFINITY;
  const sequence = Number(current.seq);
  return Number.isFinite(sequence) ? sequence : state.playbackIndex;
}

function eventsAtPlayback() {
  if (!state.timeline.length) return [];
  const sequence = playbackSequence();
  return state.timeline.filter((event, index) => {
    const eventSequence = Number(event.seq);
    const visible = Number.isFinite(eventSequence) ? eventSequence <= sequence : index <= state.playbackIndex;
    return visible || (event.kind === "score" && state.scoreOverlayIds.has(event.event_id));
  });
}

function mergeTimelineEvents(events) {
  const existing = new Set(state.timeline.map((event) => event.event_id).filter(Boolean));
  const additions = [];
  for (const event of events || []) {
    if (!event || (event.event_id && existing.has(event.event_id))) continue;
    additions.push(event);
    if (event.event_id) existing.add(event.event_id);
  }
  if (!additions.length) return additions;
  state.timeline.push(...additions);
  state.timeline.sort((left, right) => (Number(left.seq) || 0) - (Number(right.seq) || 0));
  return additions;
}

function markPlaybackFrozen() {
  state.playbackFreezeAt = Date.now();
}

function stopLiveScreenQueue({ jumpToLatest = false } = {}) {
  clearTimeout(state.liveScreenTimer);
  state.liveScreenTimer = null;
  state.liveScreenQueue = [];
  state.liveScreenEvent = null;
  if (jumpToLatest) renderTerminal();
}

function advanceLiveScreenQueue() {
  if (!state.followLive) {
    stopLiveScreenQueue();
    return;
  }
  if (!state.liveScreenQueue.length) {
    state.liveScreenTimer = null;
    state.liveScreenEvent = null;
    renderTerminal();
    return;
  }
  state.liveScreenEvent = state.liveScreenQueue.shift();
  renderTerminal();
  const frameDelay = eventDelay(state.liveScreenEvent, state.liveScreenQueue.length);
  state.liveScreenTimer = setTimeout(advanceLiveScreenQueue, frameDelay);
}

function enqueueLiveScreens(events) {
  // During takeover the terminal is byte-streamed; journal frames are not
  // queued for paced painting (the capture worker still records them for the
  // Agent after release).
  if (takeoverStream.active || !state.followLive) return;
  const screens = events.filter((event) => event.kind === "screen");
  if (!screens.length) return;
  if (screens.length > MAX_LIVE_SCREEN_QUEUE) {
    // A burst of backlog after a quiet stretch: pacing the whole batch would
    // replay it slowly and look like seconds of latency. Snap straight to the
    // newest captured frame instead.
    stopLiveScreenQueue();
    state.liveScreenEvent = screens.at(-1);
    state.liveScreenQueue = [];
    renderTerminal();
    return;
  }
  state.liveScreenQueue.push(...screens);
  if (state.liveScreenQueue.length > MAX_LIVE_SCREEN_QUEUE) {
    const source = state.liveScreenQueue;
    const stride = Math.ceil(source.length / (MAX_LIVE_SCREEN_QUEUE - 1));
    state.liveScreenQueue = source.filter((_event, index) => index % stride === 0);
    if (state.liveScreenQueue.at(-1) !== source.at(-1)) state.liveScreenQueue.push(source.at(-1));
  }
  if (!state.liveScreenTimer) advanceLiveScreenQueue();
}

function renderMeetingList() {
  el.meeting_list.replaceChildren();
  el.meeting_count.textContent = String(state.meetings.length);
  el.list_empty.hidden = state.meetings.length > 0;
  for (const meeting of state.meetings) {
    const button = node("button", `meeting-item${meeting.meeting_id === state.selectedId ? " selected" : ""}`);
    button.type = "button";
    const top = node("span", "meeting-item-top");
    top.append(node("strong", "", meeting.meeting_id), node("span", `mini-status ${meeting.status}`, labels.status[meeting.status] || meeting.status));
    const provider = labels.provider[meeting.agent_provider] || meeting.agent_provider || "OpenAI";
    button.append(top, node("span", "meeting-item-meta", `${withAgent(labels.phase[meeting.phase] || meeting.phase, meeting)} · ${provider} · ${fmtTime(meeting.updated_at)}`));
    button.addEventListener("click", () => selectMeeting(meeting.meeting_id));
    el.meeting_list.append(button);
  }
}

// A live DOM selection must never be clobbered by a periodic redraw: the
// 1s header tick and the event poll both rebuild text nodes, which would
// collapse whatever the user is trying to select/copy (e.g. a meeting id
// in the top bar). While a selection exists (or a pointer drag is in
// progress) auto-redraws stand down; they resume on the next tick after
// the selection is cleared.
function hasLiveSelection() {
  const selection = window.getSelection && window.getSelection();
  return Boolean(selection && selection.rangeCount > 0 && !selection.isCollapsed);
}

function renderHeader() {
  if (!state.snapshot) return;
  if (state.pointerSelecting || hasLiveSelection()) return;
  const meeting = state.snapshot.meeting;
  el.meeting_title.textContent = meeting.meeting_id;
  if (el.agent_states) {
    renderMemberStates(meeting);
  }
  if (el.status_badge) {
    el.status_badge.textContent = labels.status[meeting.status] || meeting.status;
    el.status_badge.dataset.status = meeting.status;
  }
  if (el.owner_badge) {
    el.owner_badge.textContent = labels.owner[meeting.control_owner] || meeting.control_owner;
  }
  const errorPhases = ["api_error", "cycle_timeout", "failed"];
  const failure = typeof meeting.failure === "string" ? meeting.failure.trim() : "";
  const inErrorPhase = errorPhases.includes(meeting.phase);
  if (failure && inErrorPhase) {
    el.failure_message.textContent = failure;
    el.failure_message.hidden = false;
  } else {
    el.failure_message.hidden = true;
  }
  el.phase_label.textContent = withAgent(labels.phase[meeting.phase] || meeting.phase, meeting);
  el.backend_label.textContent = labels.backend[meeting.agent_backend] || meeting.agent_backend || "Responses API";
  el.provider_label.textContent = labels.provider[meeting.agent_provider] || meeting.agent_provider || "OpenAI";
  el.model_label.textContent = meeting.model || "默认（CLI）";
  el.typing_profile_label.textContent = labels.typing[meeting.typing_profile] || "平衡";
  if (el.effort_live_select) {
    if (document.activeElement !== el.effort_live_select) {
      el.effort_live_select.value = meeting.reasoning_effort || "";
    }
    el.effort_live_select.disabled = ["completed", "failed", "interrupted"].includes(meeting.status);
  }
  el.action_label.textContent = `${meeting.cycle_action_count || 0} / 60`;
  el.runtime_label.textContent = duration(meeting.created_at);
  el.event_count.textContent = String(meeting.event_count || state.timeline.length);

  const terminal = state.snapshot.latest_screen?.data || {};
  const displayedTerminal = terminal;
  el.terminal_command.textContent = displayedTerminal.current_command || "shell";
  el.terminal_path.textContent = displayedTerminal.current_path
    ? displayedTerminal.current_path.split("/").slice(-2).join("/")
    : "workspace";
  if (el.control_label) {
    el.control_label.textContent = meeting.control_requested === "human" ? `等待 ${agentName(meeting)} 交出控制权` : withAgent(labels.owner[meeting.control_owner] || "无人控制", meeting);
  }

  const terminalState = ["completed", "failed", "interrupted"].includes(meeting.status);
  const evaluateAction = el.selection_menu?.querySelector('[data-selection-action="evaluate"]');
  const addAction = el.selection_menu?.querySelector('[data-selection-action="add-to-conversation"]');
  // Historical sessions remain rateable; only adding a new chat reference is
  // blocked once the meeting is terminal because the composer is read-only.
  // Human-authored chat is never rateable, so keep the evaluate button in
  // sync with the current selection even when the meeting re-renders.
  if (evaluateAction) {
    const humanChat = selectionIsHumanChat(state.pendingSelection);
    evaluateAction.disabled = humanChat;
    evaluateAction.title = humanChat ? "人类消息不能被评价" : "";
  }
  if (addAction) addAction.disabled = terminalState;
  el.message_input.disabled = terminalState;
  el.send_message_button.disabled = terminalState;
  el.delivery_mode?.querySelectorAll("input").forEach((input) => { input.disabled = terminalState; });
  if (el.evaluation_submit_button) el.evaluation_submit_button.disabled = false;
  el.complete_button.disabled = terminalState || ["created", "starting"].includes(meeting.status) || meeting.phase === "starting";
  el.stop_button.disabled = terminalState;
  el.complete_button.hidden = terminalState;
  el.stop_button.hidden = terminalState;
  el.cleanup_button.hidden = !terminalState || meeting.session_state === "stopped";
  el.resume_button.hidden = !["api_error", "cycle_timeout", "waiting_for_user", "completion_proposed"].includes(meeting.phase) || terminalState;
  el.resume_button.textContent = meeting.phase === "completion_proposed" ? "继续工作" : `继续 ${agentName(meeting)}`;
  const takeoverLive = meeting.control_owner === "human" && meeting.phase === "human_control";
  const waitingHandoff = meeting.control_requested === "human" && meeting.control_owner !== "human";
  const canAcquire = !terminalState && meeting.status === "running"
    && ["agent_thinking", "agent_acting", "waiting_for_user", "completion_proposed", "api_error", "cycle_timeout"].includes(meeting.phase);
  el.acquire_button.hidden = terminalState || takeoverLive;
  el.acquire_button.disabled = !canAcquire;
  el.acquire_button.textContent = waitingHandoff ? "等待交权…" : "接管";
  el.acquire_button.title = canAcquire ? `打断 ${agentName(meeting)} 并接管共享终端` : "";
  el.release_button.hidden = !takeoverLive;
  const terminalReadonly = el.terminal_readonly;
  if (terminalReadonly) {
    terminalReadonly.textContent = takeoverLive ? "已接管 · 实时终端" : "只读预览";
    terminalReadonly.classList.toggle("takeover-live", takeoverLive);
  }
  el.message_hint.textContent = takeoverLive
    ? `接管中：发消息 = 交还控制权并让 ${agentName(meeting)} 继续`
    : "Enter 发送 · Shift/⌘+Enter 换行 · 消息在下一个动作边界生效";
  el.message_input.placeholder = takeoverLive
    ? `接管中：输入消息会结束接管并交给 ${agentName(meeting)}…`
    : `继续说明、纠正或打断 ${agentName(meeting)}…`;
  syncTerminalTakeover(takeoverLive);
}

function visibleChats() {
  // The chat transcript is never clipped by the terminal playback cursor:
  // the scrubber replays screens, while the conversation stays fully
  // readable both before and after the current point.
  return state.timeline.filter((event) => {
    if (event.kind !== "chat") return false;
    if (state.chatFilter === "result") return ["request", "question", "final"].includes(event.message_type);
    if (state.chatFilter === "progress") return ["ack", "progress"].includes(event.message_type);
    return true;
  });
}

function codePoints(value) {
  return Array.from(String(value ?? ""));
}

function codePointLength(value) {
  return codePoints(value).length;
}

function codePointSlice(value, start, end) {
  return codePoints(value).slice(start, end).join("");
}

function textOffsetFromPoint(root, container, offset) {
  if (!root || !container || !root.contains(container)) return null;
  if (container.nodeType === 1) {
    try {
      const before = document.createRange();
      before.selectNodeContents(root);
      before.setEnd(container, Number(offset) || 0);
      return codePointLength(before.toString());
    } catch (_error) {
      return null;
    }
  }
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let total = 0;
  let current = walker.nextNode();
  while (current) {
    if (current === container) {
      const value = current.nodeValue || "";
      const prefix = codePoints(value).slice(0, Math.max(0, Number(offset) || 0));
      return total + prefix.length;
    }
    total += codePointLength(current.nodeValue || "");
    current = walker.nextNode();
  }
  return null;
}

function sourceEventForRoot(root) {
  const eventId = root?.dataset?.eventId;
  if (!eventId) return null;
  const fromTimeline = state.timeline.find(
    (event) => event.event_id === eventId && (event.kind === "chat" || event.kind === "screen"),
  );
  if (fromTimeline) return fromTimeline;
  const latest = state.snapshot?.latest_screen;
  return latest?.event_id === eventId ? latest : null;
}

function scoreAnnotationsForSource(source, visibleEvents = eventsAtPlayback()) {
  if (!source) return [];
  const latestByRegion = new Map();
  for (const event of visibleEvents) {
    if (event.kind !== "score") continue;
    const data = event.data && typeof event.data === "object" ? event.data : {};
    const reference = data.reference;
    const region = reference?.region;
    if (
      reference?.event_id !== source.event_id
      || (reference?.kind && reference.kind !== source.kind)
      || !region
      || (data.tag !== "competent" && data.tag !== "incompetent")
    ) continue;
    const start = Number(region.start);
    const end = Number(region.end);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start) continue;
    const key = `${start}:${end}`;
    const sequence = Number(event.seq) || 0;
    const previous = latestByRegion.get(key);
    if (previous && previous.seq > sequence) continue;
    latestByRegion.set(key, {
      start,
      end,
      tag: data.tag,
      judge_name: typeof data.judge_name === "string" ? data.judge_name : "human",
      comment: typeof data.comment === "string" ? data.comment : "",
      seq: sequence,
      event_id: event.event_id,
    });
  }
  return [...latestByRegion.values()]
    .filter((item) => item.tag === "competent" || item.tag === "incompetent")
    .sort((left, right) => left.start - right.start || left.end - right.end || left.seq - right.seq);
}

function scoreReferenceAtPlayback() {
  if (state.followLive) return null;
  const event = eventAt(state.playbackIndex);
  const reference = event?.kind === "score" ? event.data?.reference : null;
  if (
    !reference
    || (reference.kind !== "chat" && reference.kind !== "screen")
    || typeof reference.event_id !== "string"
  ) return null;
  return reference;
}

function appendAnnotatedText(root, content, annotations) {
  const text = String(content ?? "");
  const chars = codePoints(text);
  if (!annotations.length) {
    root.append(document.createTextNode(text));
    return;
  }
  const boundaries = new Set([0, chars.length]);
  for (const annotation of annotations) {
    const start = Math.max(0, Math.min(chars.length, annotation.start));
    const end = Math.max(start, Math.min(chars.length, annotation.end));
    if (end > start) boundaries.add(start), boundaries.add(end);
  }
  const points = [...boundaries].sort((left, right) => left - right);
  for (let index = 0; index < points.length - 1; index += 1) {
    const start = points[index];
    const end = points[index + 1];
    if (end <= start) continue;
    const active = annotations.filter((item) => item.start <= start && item.end >= end);
    const segment = chars.slice(start, end).join("");
    if (!active.length) {
      root.append(document.createTextNode(segment));
      continue;
    }
    const tags = [...new Set(active.map((item) => item.tag))].sort();
    const mark = node(
      "span",
      `score-annotation ${tags.map((tag) => `score-${tag}`).join(" ")}${active.length > 1 ? " score-overlap" : ""}`,
      segment,
    );
    mark.dataset.scoreTags = tags.join(" ");
    mark.dataset.scoreCount = String(active.length);
    mark.title = tags.map((tag) => labels.score[tag] || tag).join(" / ");
    root.append(mark);
  }
}

function scoreSummary(annotations) {
  if (!annotations.length) return null;
  const summary = node("div", "score-summary");
  for (const annotation of annotations) {
    const judge = annotation.judge_name ? ` · ${annotation.judge_name}` : "";
    const badge = node("span", `score-badge ${annotation.tag}`, `${labels.score[annotation.tag] || annotation.tag}${judge}`);
    if (annotation.comment) badge.title = annotation.comment;
    summary.append(badge);
  }
  return summary;
}

function referencesForEvent(event) {
  const data = event?.data && typeof event.data === "object" ? event.data : {};
  const raw = Array.isArray(data.references)
    ? data.references
    : (data.reference ? [data.reference] : []);
  const references = [];
  const seen = new Set();
  for (const reference of raw) {
    if (!reference || typeof reference !== "object") continue;
    const region = reference.region && typeof reference.region === "object" ? reference.region : {};
    const key = `${reference.kind || ""}:${reference.event_id || ""}:${region.start ?? ""}:${region.end ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    references.push(reference);
  }
  return references;
}

function jumpToChatTime(chatEvent) {
  const targetTime = Date.parse(chatEvent.captured_at || "");
  let screenIndex = -1;
  state.timeline.forEach((item, index) => {
    if (item.kind !== "screen") return;
    const itemTime = Date.parse(item.captured_at || "");
    if (Number.isFinite(targetTime) && Number.isFinite(itemTime) && itemTime <= targetTime) {
      screenIndex = index;
    }
  });
  if (screenIndex < 0) {
    screenIndex = state.timeline.findIndex((item) => item.kind === "screen");
  }
  if (screenIndex < 0) return;
  state.scoreOverlayIds.clear();
  hidePlaybackHover();
  markPlaybackFrozen();
  state.followLive = false;
  state.chatFollow = false;
  state.playbackIndex = screenIndex;
  state.viewportMode = "free";
  stopLiveScreenQueue();
  stopPlayback();
  // Clear any lingering selection/menu state so renderAll is not suppressed.
  state.pendingSelection = null;
  state.selectionTimer = null;
  state.selectionMenuDelay = 0;
  state.pointerSelecting = false;
  state.keyboardSelection = null;
  clearBrowserSelection();
  hideSelectionMenu(false);
  renderViewportMode();
  renderAll();
}

function renderChat() {
  if (activeSourceSelection()) return;
  const previousScrollTop = el.chat_list.scrollTop;
  const replayReference = scoreReferenceAtPlayback();
  let replayTarget = null;
  let cursorArticle = null;
  // Playback chat cursor: the transcript is never clipped, but during
  // playback the last message at or before the current replay point is
  // marked so the reader (and follow-scroll) always know where the replay
  // stands inside the full conversation.
  const chatCursorSeq = state.followLive ? Number.POSITIVE_INFINITY : playbackSequence();
  state.renderingChat = true;
  try {
    el.chat_list.replaceChildren();
    const chats = visibleChats();
    // Score overlays annotate the transcript wherever the evaluation marker
    // lives; chat text stays annotated regardless of the playback cursor.
    const visibleScores = state.timeline.filter(
      (event) => event.kind === "score" && state.scoreOverlayIds.has(event.event_id),
    );
    for (const event of chats) {
      const article = node("article", `message ${event.actor}`);
      article.dataset.eventId = event.event_id || "";
      const head = node("header", "message-head");
      const timeEl = node("time", "", fmtTime(event.captured_at));
      timeEl.title = "跳转到相近终端画面";
      timeEl.addEventListener("click", () => jumpToChatTime(event));
      const meetingSnapshot = state.snapshot?.meeting;
      // Show the record's own speaker names: human = 你, every agent (and
      // future role-map member) labels by its actor name as recorded.
      const speaker = event.actor === "human"
        ? "你"
        : (event.actor || agentName(meetingSnapshot));
      head.append(
        node("strong", "", speaker),
        node("span", `message-type ${event.message_type || ""}`, labels.message[event.message_type] || "消息"),
        timeEl,
      );
      const annotations = scoreAnnotationsForSource(event, visibleScores);
      const body = node("p", "message-content");
      body.dataset.eventId = event.event_id || "";
      body.tabIndex = 0;
      body.setAttribute("aria-label", "聊天消息文本");
      appendAnnotatedText(body, event.content, annotations);
      article.append(head, body);
      const summary = scoreSummary(annotations);
      if (summary) article.append(summary);
      if (replayReference?.kind === "chat" && replayReference.event_id === event.event_id) {
        article.classList.add("score-replay-target");
        replayTarget = article;
      }
      const references = referencesForEvent(event);
      references.forEach((reference, referenceIndex) => {
        if (!reference?.excerpt) return;
        const prefix = references.length > 1 ? `引用 ${referenceIndex + 1} · ` : "引用 · ";
        const quote = node("blockquote", "message-reference", `${prefix}${reference.excerpt}`);
        quote.title = "跳转到引用来源";
        quote.addEventListener("click", () => {
          const index = state.timeline.findIndex((item) => item.event_id === reference.event_id);
          if (index < 0) return;
          state.scoreOverlayIds.clear();
          hidePlaybackHover();
          markPlaybackFrozen();
          state.followLive = false;
          state.playbackIndex = index;
          state.highlightReference = reference;
          stopLiveScreenQueue();
          stopPlayback();
          renderAll();
          const activeQuote = [...el.chat_list.querySelectorAll(".message-reference")]
            .find((item) => item.closest("article")?.dataset.eventId === event.event_id && item.textContent?.includes(reference.excerpt));
          activeQuote?.classList.add("highlighted");
          setTimeout(() => activeQuote?.classList.remove("highlighted"), 900);
        });
        if (state.highlightReference?.event_id === reference.event_id) quote.classList.add("highlighted");
        article.append(quote);
      });
      el.chat_list.append(article);
      const eventSequence = Number(event.seq);
      if (
        !state.followLive
        && Number.isFinite(eventSequence)
        && eventSequence <= chatCursorSeq
      ) {
        cursorArticle = article;
      }
    }
    let cursorDivider = null;
    if (!state.followLive && cursorArticle) {
      cursorDivider = node("div", "chat-playback-cursor");
      cursorDivider.append(node("span", "", "▶ 播放到此处"));
      cursorArticle.insertAdjacentElement("afterend", cursorDivider);
    }
    if (!chats.length) el.chat_list.append(node("div", "empty-chat", "还没有聊天消息"));
    // All scrollTop writes go through here so the follow-scroll renderChat
    // applies can be told apart from a genuine user scroll: the chat scroll
    // handler must not pause following because of its own programmatic scroll.
    const scrollChatTo = (value) => {
      const maxScroll = Math.max(0, el.chat_list.scrollHeight - el.chat_list.clientHeight);
      const target = Math.max(0, Math.min(value, maxScroll));
      state.chatScrollApplied = target;
      el.chat_list.scrollTop = target;
    };
    if (replayTarget) {
      const listRect = el.chat_list.getBoundingClientRect();
      const targetRect = replayTarget.getBoundingClientRect();
      const targetTop = el.chat_list.scrollTop + targetRect.top - listRect.top;
      scrollChatTo(targetTop - (el.chat_list.clientHeight - targetRect.height) / 2);
    } else if (state.chatFollow) {
      if (state.followLive || !cursorDivider) {
        scrollChatTo(el.chat_list.scrollHeight);
      } else {
        // Keep the playback cursor line pinned near the bottom while the
        // transcript keeps scrolling above it as the replay advances.
        const listRect = el.chat_list.getBoundingClientRect();
        const dividerRect = cursorDivider.getBoundingClientRect();
        const dividerTop = el.chat_list.scrollTop + dividerRect.top - listRect.top;
        scrollChatTo(dividerTop + dividerRect.height + 16 - el.chat_list.clientHeight);
      }
    } else {
      scrollChatTo(previousScrollTop);
    }
  } finally {
    state.renderingChat = false;
  }
}

let terminalEngine = null;
let lastTerminalPlainText = "";
let lastTerminalAnsiText = "";
let lastTerminalFrame = null; // { event_id, content } of the painted frame
let lastTerminalSelectionPoint = null; // { x, y } client coords

function copyFallbackDialog(text) {
  // Last-resort copy: a small modal with the text already selected, so a
  // plain Cmd/Ctrl+C always works even when the clipboard API is denied.
  const overlay = node("div", "copy-fallback-overlay");
  const panel = node("div", "copy-fallback-panel");
  panel.append(node("p", "", "浏览器阻止了自动复制，请直接按 ⌘/Ctrl+C（文本已全选）"));
  const area = node("textarea", "copy-fallback-area");
  area.readOnly = true;
  area.value = text;
  area.addEventListener("focus", () => area.select());
  const closeButton = node("button", "primary-command compact", "关闭");
  closeButton.type = "button";
  closeButton.addEventListener("click", () => overlay.remove());
  panel.append(area, closeButton);
  overlay.append(panel);
  overlay.addEventListener("pointerdown", (event) => {
    if (event.target === overlay) overlay.remove();
  });
  document.body.appendChild(overlay);
  area.focus();
  area.select();
}

function legacyCopy(text) {
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.focus();
  helper.select();
  helper.setSelectionRange(0, text.length);
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (_error) {
    copied = false;
  } finally {
    helper.remove();
  }
  return copied;
}

function copyTextToClipboard(text) {
  if (!text) {
    toast("没有可复制的内容");
    return false;
  }
  const notify = () => toast(`已复制 ${codePointLength(text)} 字符`);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(
      notify,
      () => {
        if (legacyCopy(text)) notify();
        else copyFallbackDialog(text);
      },
    );
    return true;
  }
  if (legacyCopy(text)) {
    notify();
    return true;
  }
  copyFallbackDialog(text);
  return false;
}

// Clipboard/selection helpers talk to whichever Terminal instance is live so
// they can be bound once while the terminal itself is rebuilt on mode
// switches (takeover stream <-> captured-frame painting).
let terminalClipboardBound = false;

function activeTerm() {
  return terminalEngine ? terminalEngine.term : null;
}

function installTerminalClipboard() {
  if (terminalClipboardBound) return;
  terminalClipboardBound = true;
  // Entering the terminal with a click gives xterm focus so its own
  // selection machinery (drag, double/triple click) is active.
  el.terminal_screen.addEventListener("pointerdown", () => {
    const term = activeTerm();
    if (!term) return;
    try {
      term.focus();
    } catch (_error) {
      // focus may be no-op before the terminal is fully open.
    }
  });

  // Double-click / triple-click: xterm selects word / line internally. Confirm
  // it actually happened so a missing highlight is not mistaken for a dead
  // gesture.
  let multiClickTimer = null;
  el.terminal_screen.addEventListener("pointerup", (event) => {
    const term = activeTerm();
    lastTerminalSelectionPoint = { x: event.clientX, y: event.clientY };
    if (!term || !event.detail || event.detail < 2) return;
    clearTimeout(multiClickTimer);
    multiClickTimer = setTimeout(() => {
      const position = typeof term.getSelectionPosition === "function" ? term.getSelectionPosition() : null;
      const selected = term.getSelection();
      if (position && selected) {
        toast(`已选中 ${selected.length} 字符 · ⌘/Ctrl+C 复制`);
      }
    }, 30);
  });

  // Selected text copies with Cmd/Ctrl+C (and Cmd/Ctrl+Shift+C).
  el.terminal_screen.addEventListener("keydown", (event) => {
    if (!(event.metaKey || event.ctrlKey)) return;
    const key = event.key.toLowerCase();
    if (key !== "c") return;
    const term = activeTerm();
    if (!term) return;
    const selected = term.getSelection();
    if (!selected) return;
    event.preventDefault();
    copyTextToClipboard(selected);
  });
  // Right-click copies the selection, or the whole current frame when nothing
  // is selected. Shift+right-click copies the raw ANSI of the current frame
  // so the color layer can be inspected or pasted for debugging.
  el.terminal_screen.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    if (event.shiftKey) {
      copyTextToClipboard(lastTerminalAnsiText || lastTerminalPlainText);
      return;
    }
    const term = activeTerm();
    const selected = term ? term.getSelection() : "";
    copyTextToClipboard(selected || lastTerminalPlainText);
  });
}

function ensureTerminalEngine() {
  // Lazily mount a single xterm.js terminal into #terminal-screen. Returns
  // null when xterm.js is unavailable so the legacy DOM text path is used.
  if (terminalEngine) return terminalEngine.term;
  if (!el.terminal_screen || typeof window.Terminal !== "function") return null;
  let term;
  try {
    term = new window.Terminal({
      allowProposedApi: true,
      disableStdin: true,
      convertEol: true,
      cursorBlink: true,
      cursorStyle: "block",
      // An unfocused xterm draws its cursor as a faint outline by default,
      // which is invisible on the dark theme while playback/scrubbing (no
      // keyboard focus). Keep the solid block cursor at all times.
      cursorInactiveStyle: "block",
      fontSize: TERMINAL_FONT_SIZE,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      lineHeight: TERMINAL_LINE_HEIGHT,
      scrollback: 0,
      theme: {
        background: "#151817",
        foreground: "#d5dbd6",
        cursor: "#c5cdc8",
        selectionBackground: "#5b7fd4",
        selectionForeground: "#ffffff",
        black: "#1c1f1e", red: "#e06c75", green: "#98c379", yellow: "#d19a66",
        blue: "#61afef", magenta: "#c678dd", cyan: "#56b6c2", white: "#abb2bf",
        brightBlack: "#5c6370", brightRed: "#e06c75", brightGreen: "#98c379",
        brightYellow: "#d19a66", brightBlue: "#61afef", brightMagenta: "#c678dd",
        brightCyan: "#56b6c2", brightWhite: "#ffffff",
      },
    });
    el.terminal_screen.replaceChildren();
    term.open(el.terminal_screen);
    installTerminalClipboard();
  } catch (_error) {
    return null;
  }
  terminalEngine = { term };
  // xterm needs a paint before its real cell metrics exist; measure again a
  // moment after mount so the very first layout already uses real metrics
  // instead of the nominal estimate (which clips rows on some displays).
  setTimeout(() => {
    if (!terminalEngine) return;
    terminalEngine.term._cellHeight = null;
    state.terminalFrameKey = null;
    renderTerminal();
  }, 350);
  return term;
}

// Rebuild the xterm from scratch. The two rendering modes -- captured-frame
// painting (normal/Agent) and the byte-stream takeover -- must never share a
// lingering screen buffer: painting over stream leftovers or seeding over
// painted leftovers is exactly what produced duplicated rows and scrambled
// columns on release/re-acquire. A fresh Terminal instance makes every mode
// switch start from a physically empty screen.
function rebuildTerminalEngine() {
  if (terminalEngine) {
    try {
      terminalEngine.term.dispose();
    } catch (_error) {
      // dispose may throw if the terminal is mid-render; ignore.
    }
    terminalEngine = null;
  }
  return ensureTerminalEngine();
}

// Re-measure the real cell box and repaint when the terminal's container size
// changes (window resize, or the takeover panel appearing/disappearing under
// it). Row count is derived from the measured height, never hard-coded.
function refitTerminal() {
  if (!terminalEngine) return;
  terminalEngine.term._cellHeight = null;
  termRowsState = null;
  state.terminalFrameKey = null;
  renderTerminal();
}

// Tracks takeover-panel visibility between renders so the terminal refits
// exactly when the panel appears or disappears (not on every poll).
// ---- True-terminal takeover (byte stream) ----
// While the human owns the pane, the xterm screen becomes a real terminal:
// server output (tmux pipe-pane) streams in over SSE and keystrokes are sent
// back byte-for-byte through /terminal/input and recorded in the journal.
const takeoverStream = {
  active: false,
  source: null,
  inputHex: "",
  strokes: [],
  flushTimer: null,
  flushing: false,
  lastFlushErrorAt: 0,
  keyHandler: null,
  compositionHandler: null,
  pasteHandler: null,
};

function decodeBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function flushTakeoverInput(silent = false) {
  takeoverStream.flushTimer = null;
  if (!takeoverStream.inputHex || takeoverStream.flushing) return;
  const hex = takeoverStream.inputHex;
  const strokes = takeoverStream.strokes;
  takeoverStream.inputHex = "";
  takeoverStream.strokes = [];
  takeoverStream.flushing = true;
  const body = { bytes: hex };
  if (strokes.length) body.strokes = strokes;
  api(`/api/meetings/${encodeURIComponent(state.selectedId || "")}/terminal/input`, {
    method: "POST",
    body: JSON.stringify(body),
  }).catch(() => {
    if (!silent) {
      const now = Date.now();
      if (now - takeoverStream.lastFlushErrorAt > 2500) {
        takeoverStream.lastFlushErrorAt = now;
        toast("终端输入发送失败：连接已断开？");
      }
    }
  }).finally(() => {
    takeoverStream.flushing = false;
    // Anything queued while the request was in flight must still go out.
    if (takeoverStream.inputHex && takeoverStream.flushTimer === null) {
      takeoverStream.flushTimer = setTimeout(() => flushTakeoverInput(false), 15);
    }
  });
}

// Control keys (Enter, Backspace, arrows, Ctrl+letter) must feel instant:
// they flush immediately instead of waiting for the typing batch window.
function takeoverBytesUrgent(text) {
  for (const char of text) {
    const code = char.charCodeAt(0);
    if (code < 0x20 || code === 0x7f) return true;
  }
  return false;
}

function queueTakeoverInput(text, urgent = false) {
  const bytes = new TextEncoder().encode(text);
  let hex = "";
  for (const byte of bytes) hex += byte.toString(16).padStart(2, "0");
  takeoverStream.inputHex += hex;
  // One journaled stroke per physical input event (keystroke, IME commit,
  // or paste), stamped with the moment the user actually typed it.
  takeoverStream.strokes.push({ hex, stroke_time: new Date().toISOString() });
  if (takeoverStream.flushTimer !== null) {
    if (!urgent) return;
    clearTimeout(takeoverStream.flushTimer);
    takeoverStream.flushTimer = null;
  }
  takeoverStream.flushTimer = setTimeout(
    () => flushTakeoverInput(false),
    urgent ? 0 : 30,
  );
}

// The live terminal must match the pane exactly, so the pane is resized to
// this grid before the stream opens (real clients resize the pty to the
// client). Rows/cols are derived from the measured panel and cell box, never
// guessed.
function takeoverGrid(engine) {
  const panelWidth = el.terminal_screen.clientWidth || 0;
  const panelHeight = el.terminal_screen.clientHeight || 0;
  let charWidth = Number.NaN;
  try {
    charWidth = Number(engine?._core?._renderService?.dimensions?.css?.cell?.width);
  } catch (_error) {
    // fall through to the estimate
  }
  if (!Number.isFinite(charWidth) || charWidth <= 0) charWidth = TERMINAL_FONT_SIZE * 0.6;
  const cellHeight = terminalCellHeight(engine);
  let cols = 80;
  if (panelWidth > 0 && charWidth > 0) cols = Math.max(40, Math.min(300, Math.floor(panelWidth / charWidth)));
  let rows = 24;
  if (panelHeight > 0 && cellHeight > 0) rows = Math.floor((panelHeight - TERMINAL_BOTTOM_RESERVE_PX) / cellHeight);
  rows = Math.max(12, Math.min(120, rows));
  return { cols, rows };
}

const takeoverKeymap = {
  Enter: "\r",
  Backspace: "\x7f",
  Tab: "\t",
  Escape: "\x1b",
  ArrowUp: "\x1b[A",
  ArrowDown: "\x1b[B",
  ArrowRight: "\x1b[C",
  ArrowLeft: "\x1b[D",
  Home: "\x1b[H",
  End: "\x1b[F",
  Insert: "\x1b[2~",
  Delete: "\x1b[3~",
  PageUp: "\x1b[5~",
  PageDown: "\x1b[6~",
};

// Keys we own while takeover is live. xterm's own keyboard path is disabled
// (disableStdin) because several of its handlers can each emit the same key;
// every physical key must reach the pane exactly once. Returns bytes or null
// when the browser should keep the event (meta shortcuts, IME, modifiers).
function takeoverKeyBytes(event) {
  if (event.metaKey || event.altKey) return null;
  if (event.ctrlKey) {
    const key = event.key.toLowerCase();
    if (key.length === 1 && key >= "a" && key <= "z") {
      return String.fromCharCode(key.charCodeAt(0) - 96);
    }
    const extras = {
      " ": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d",
      "^": "\x1e", "_": "\x1f", "2": "\x00", "6": "\x1e",
      "-": "\x1f", "/": "\x1f", "@": "\x00",
    };
    return Object.prototype.hasOwnProperty.call(extras, event.key) ? extras[event.key] : null;
  }
  if (Object.prototype.hasOwnProperty.call(takeoverKeymap, event.key)) return takeoverKeymap[event.key];
  if (event.key.length === 1) return event.key;
  return null;
}

function takeoverInputInScreen(target) {
  return Boolean(el.terminal_screen && target instanceof Node && el.terminal_screen.contains(target));
}

function bindTakeoverInput(engine) {
  takeoverStream.keyHandler = (event) => {
    if (!takeoverStream.active || !takeoverInputInScreen(event.target)) return;
    if (event.isComposing || event.keyCode === 229) return; // IME composing
    if (event.metaKey) return; // leave ⌘ shortcuts (copy/paste) alone
    if (event.ctrlKey && event.shiftKey && ["c", "v"].includes(event.key.toLowerCase())) return;
    if (event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === "c") {
      const term = activeTerm();
      if (term && typeof term.hasSelection === "function" && term.hasSelection()) return; // copy, not SIGINT
    }
    const bytes = takeoverKeyBytes(event);
    if (bytes === null) return;
    event.preventDefault();
    event.stopPropagation();
    queueTakeoverInput(bytes, takeoverBytesUrgent(bytes));
  };
  takeoverStream.compositionHandler = (event) => {
    if (!takeoverStream.active || !takeoverInputInScreen(event.target)) return;
    if (event.type === "compositionend" && event.data) queueTakeoverInput(event.data, takeoverBytesUrgent(event.data));
  };
  takeoverStream.pasteHandler = (event) => {
    if (!takeoverStream.active || !takeoverInputInScreen(event.target)) return;
    const text = event.clipboardData ? event.clipboardData.getData("text/plain") : "";
    if (!text) return;
    event.preventDefault();
    queueTakeoverInput(text, takeoverBytesUrgent(text));
  };
  for (const [type, handler] of [
    ["keydown", takeoverStream.keyHandler],
    ["compositionend", takeoverStream.compositionHandler],
    ["paste", takeoverStream.pasteHandler],
  ]) {
    document.addEventListener(type, handler, true);
  }
  try { engine.focus(); } catch (_error) { /* no-op */ }
}

function unbindTakeoverInput() {
  for (const [type, key] of [
    ["keydown", "keyHandler"],
    ["compositionend", "compositionHandler"],
    ["paste", "pasteHandler"],
  ]) {
    const handler = takeoverStream[key];
    if (handler) {
      document.removeEventListener(type, handler, true);
      takeoverStream[key] = null;
    }
  }
}

function startTerminalTakeover() {
  if (takeoverStream.active) return;
  // Measure the viewport grid on the CURRENT engine (its cell metrics are
  // already real from frame painting), then hand the stream a brand-new
  // Terminal so no painted cells survive into the byte-stream mode.
  const previous = ensureTerminalEngine();
  const grid = previous ? takeoverGrid(previous) : { cols: 80, rows: 24 };
  const engine = rebuildTerminalEngine();
  if (!engine) return;
  takeoverStream.active = true;
  takeoverRepaintPending = false;
  takeoverRepaintForceReset = false;
  stopLiveScreenQueue();
  // xterm's own keyboard path stays OFF (disableStdin): multiple internal
  // handlers could each emit the same key, doubling every character. We
  // translate keys ourselves in document capture and send exactly once.
  engine.options.disableStdin = true;
  // convertEol stays TRUE: the seed frame separates rows with bare LF and
  // the emulator must turn LF into CRLF or every new row starts where the
  // previous row ended (lines drift right). A real CRLF in the stream only
  // adds a harmless extra carriage return.
  engine.options.convertEol = true;
  // No browser-side scrollback during takeover: the viewport equals the
  // pane's own screen (which scrolls internally), and a vertical scrollbar
  // would steal gutter width and push the rightmost columns underneath it.
  engine.options.scrollback = 0;
  if (engine.cols !== grid.cols || engine.rows !== grid.rows) engine.resize(grid.cols, grid.rows);
  termRowsState = null;
  engine.reset();
  bindTakeoverInput(engine);

  const streamUrl = `/api/meetings/${encodeURIComponent(state.selectedId)}/terminal/stream?cols=${grid.cols}&rows=${grid.rows}`;
  const source = new EventSource(streamUrl);
  takeoverStream.source = source;
  source.addEventListener("size", (event) => {
    if (!takeoverStream.active) return;
    const match = /^(\d+)x(\d+)$/.exec(event.data || "");
    if (!match) return;
    const paneWidth = Number(match[1]);
    const paneHeight = Number(match[2]);
    // The pane's actual size after tmux processing is the single truth; the
    // emulator must match it exactly or erases/wraps drift.
    if (paneWidth >= 40 && paneHeight >= 12 && (engine.cols !== paneWidth || engine.rows !== paneHeight)) {
      engine.resize(paneWidth, paneHeight);
    }
  });
  source.addEventListener("seed", (event) => {
    if (!takeoverStream.active) return;
    engine.reset();
    engine.write(new Uint8Array(decodeBase64(event.data)));
    engine.scrollToBottom();
  });
  source.addEventListener("data", (event) => {
    if (!takeoverStream.active) return;
    engine.write(new Uint8Array(decodeBase64(event.data)));
    engine.scrollToBottom();
  });
  source.addEventListener("control", (event) => {
    if (!takeoverStream.active) return;
    if (event.data === "reset") engine.reset();
    if (event.data === "released") stopTerminalTakeover(false);
  });
  source.onerror = () => {
    // The server closes the stream with a 4xx when takeover ended; do not
    // let EventSource reconnect forever once control is no longer human.
    const meeting = state.snapshot?.meeting;
    if (!meeting || meeting.control_owner !== "human" || meeting.phase !== "human_control") {
      stopTerminalTakeover(false);
    }
  };
  // Bring focus in so typing starts working immediately after 接管.
  setTimeout(() => {
    if (takeoverStream.active) {
      try { engine.focus(); } catch (_error) { /* no-op */ }
    }
  }, 80);
}

function stopTerminalTakeover(flushPending) {
  if (!takeoverStream.active) return;
  takeoverStream.active = false;
  if (takeoverStream.flushTimer !== null) {
    clearTimeout(takeoverStream.flushTimer);
    takeoverStream.flushTimer = null;
  }
  if (flushPending) flushTakeoverInput(true);
  const source = takeoverStream.source;
  takeoverStream.source = null;
  if (source) source.close();
  unbindTakeoverInput();
  takeoverStream.inputHex = "";
  takeoverStream.strokes = [];
  // Hand the terminal back to frame painting from a fresh instance so no
  // byte-stream cells can linger under the painted frames. The local
  // timeline/buffer can no longer be trusted after mode switches, so block
  // every local paint, re-fetch the authoritative screen from the server,
  // and paint exactly once from that snapshot. Any local render racing this
  // transition is skipped while the fetch is pending.
  stopLiveScreenQueue();
  rebuildTerminalEngine();
  termRowsState = null;
  state.terminalFrameKey = null;
  takeoverRepaintPending = true;
  setTimeout(async () => {
    try {
      if (!takeoverStream.active) {
        await refreshSelected();
      }
    } catch (_error) {
      // paint whatever we have below rather than leaving a blank screen
    } finally {
      takeoverRepaintPending = false;
      if (!takeoverStream.active) {
        takeoverRepaintForceReset = true;
        renderTerminal();
        // Back in frame-painted live view: keep the cursor blinking.
        focusTerminalCursor();
      }
    }
  }, 0);
}

function syncTerminalTakeover(live) {
  if (live && !takeoverStream.active) {
    startTerminalTakeover();
  } else if (!live && takeoverStream.active) {
    stopTerminalTakeover(true);
  }
}

// When takeover ends, painting is suspended until the authoritative screen
// is re-fetched from the server; only that one paint is allowed, so no local
// render can stack a second copy over stream leftovers.
let takeoverRepaintPending = false;
let takeoverRepaintForceReset = false;

const TERMINAL_FONT_SIZE = 12;
const TERMINAL_LINE_HEIGHT = 1.42;
const TERMINAL_ROW_PX = TERMINAL_FONT_SIZE * TERMINAL_LINE_HEIGHT;
// Real xterm row height is slightly larger than the nominal product, so an
// exact floor() can clip the last row below the panel edge. Reserve a few
// pixels so the bottom line (and the prompt/cursor) is always fully visible.
const TERMINAL_BOTTOM_RESERVE_PX = 14;
let termRowsState = null; // { width, height, plain: string[] } of last painted frame

const TERMINAL_STYLE_CODES = { bold: "1", dim: "2", italic: "3", underline: "4", blink: "5", reverse: "7" };

function terminalColorCodes(color, foreground) {
  if (!color || typeof color !== "object") return [];
  const palette = color.palette;
  if (typeof palette === "number" && palette >= 0 && palette <= 255) {
    if (palette < 8) return [String((foreground ? 30 : 40) + palette)];
    if (palette < 16) return [String((foreground ? 90 : 100) + palette - 8)];
    return [foreground ? "38" : "48", "5", String(palette)];
  }
  const rgb = color.truecolor;
  if (rgb && typeof rgb === "object") {
    const { r, g, b } = rgb;
    if ([r, g, b].every((v) => typeof v === "number" && v >= 0 && v <= 255)) {
      return [foreground ? "38" : "48", "2", String(r), String(g), String(b)];
    }
  }
  return [];
}

function encodeRunSgr(fg, bg, styles) {
  const codes = [];
  for (const name of ["bold", "dim", "italic", "underline", "blink", "reverse"]) {
    if (Array.isArray(styles) && styles.includes(name)) codes.push(TERMINAL_STYLE_CODES[name]);
  }
  codes.push(...terminalColorCodes(fg, true), ...terminalColorCodes(bg, false));
  return codes.length ? "\x1b[" + codes.join(";") + "m" : "";
}

function buildTerminalRows(segments, plainText, height) {
  const rows = [];
  let rowPlain = "";
  let rowRuns = [];
  const pushLine = () => {
    rows.push({ plain: rowPlain, ansi: "\x1b[0m" + rowRuns.map((r) => "\x1b[0m" + encodeRunSgr(r.fg, r.bg, r.styles) + r.text).join("") });
    rowPlain = "";
    rowRuns = [];
  };
  const emit = (text, fg, bg, styles) => {
    if (!text) return;
    rowPlain += text;
    rowRuns.push({ text, fg, bg, styles });
  };
  if (Array.isArray(segments) && segments.length) {
    for (const segment of segments) {
      const { fg, bg, styles } = segment;
      const pieces = String(segment.text ?? "").split("\n");
      for (let index = 0; index < pieces.length; index += 1) {
        if (index > 0) pushLine();
        emit(pieces[index], fg, bg, styles);
      }
    }
  } else {
    for (const line of String(plainText ?? "").split("\n")) {
      if (rowPlain) pushLine();
      emit(line, null, null, []);
    }
  }
  pushLine();
  while (rows.length < height) {
    rows.push({ plain: "", ansi: "\x1b[0m" });
  }
  if (rows.length > height) return rows.slice(rows.length - height);
  return rows;
}

function paintTerminalFrame(term, rows, width, height, hasWaiting) {
  if (
    hasWaiting
    || !termRowsState
    || termRowsState.width !== width
    || termRowsState.height !== height
    || termRowsState.plain.length !== rows.length
  ) {
    // Full repaint writes every row at its absolute position. Streaming a
    // whole frame through the terminal instead would scroll by the frame's
    // own line count and drift away from the cursor/offset math.
    term.reset();
    for (let index = 0; index < rows.length; index += 1) {
      term.write(`\x1b[${index + 1};1H` + rows[index].ansi + "\x1b[K");
    }
  } else {
    const changed = [];
    for (let index = 0; index < rows.length; index += 1) {
      if (termRowsState.plain[index] !== rows[index].plain) changed.push(index);
    }
    if (changed.length && changed.length * 2 < rows.length) {
      for (const index of changed) {
        term.write(`\x1b[${index + 1};1H` + rows[index].ansi + "\x1b[K");
      }
    } else if (changed.length) {
      // Too many rows changed: a full repaint is cheaper than many moves.
      term.reset();
      for (let index = 0; index < rows.length; index += 1) {
        term.write(`\x1b[${index + 1};1H` + rows[index].ansi + "\x1b[K");
      }
    }
  }
  termRowsState = { width, height, plain: rows.map((row) => row.plain) };
}

// Real xterm cell height depends on the installed font and the display, so it
// is measured at runtime (cache per engine) instead of hard-coded; static
// guesses clip the bottom row on some machines/screens.
function terminalCellHeight(term) {
  if (term && term._cellHeight) return term._cellHeight;
  const fromDims = (() => {
    try {
      const cell = term?._core?._renderService?.dimensions?.css?.cell;
      const value = cell ? Number(cell.height) : NaN;
      return Number.isFinite(value) && value > 0 ? value : NaN;
    } catch (_error) {
      return NaN;
    }
  })();
  if (Number.isFinite(fromDims)) {
    term._cellHeight = fromDims;
    return fromDims;
  }
  try {
    const rows = term?.element?.querySelector(".xterm-rows");
    if (rows && term.rows > 0) {
      const fromDom = rows.getBoundingClientRect().height / term.rows;
      if (Number.isFinite(fromDom) && fromDom > 0) {
        term._cellHeight = fromDom;
        return fromDom;
      }
    }
  } catch (_error) {
    // fall through
  }
  // Not measurable yet (e.g. first paint): return the nominal estimate but do
  // NOT cache it, so a later render re-measures with the real cell height.
  return TERMINAL_ROW_PX;
}

// xterm only starts drawing its cursor once the cursor is "initialized",
// which normally happens on focus or key input. Captured-frame painting (live
// preview and playback) never focuses or types, so the cursor would stay
// invisible forever. Borrow focus once per engine and hand it straight back:
// the focus handler initializes the cursor, and the previous element (e.g. the
// chat composer) keeps the keyboard.
function initializeTerminalCursor(term) {
  if (terminalEngine?.cursorInitDone) return;
  if (terminalEngine) terminalEngine.cursorInitDone = true;
  try {
    const previous = document.activeElement;
    term.focus();
    if (previous && previous !== document.activeElement && typeof previous.focus === "function") {
      setTimeout(() => {
        if (document.activeElement === term.textarea) previous.focus();
      }, 0);
    }
  } catch (_error) {
    // focus may be unavailable or throw in odd states; the cursor is cosmetic.
  }
}

// xterm only blinks its cursor while its textarea holds focus. Live watching
// and takeover should blink (like a real terminal), and clicking around the UI
// (scrubber, buttons...) must not stop that: as long as the focus is not on an
// editable field, hand it back to the terminal. The chat composer and other
// text inputs are never touched, so typing a message still works normally.
function isEditableFocusTarget(node) {
  if (!node || node === document.body) return false;
  if (node.isContentEditable) return true;
  const tag = node.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = String(node.type || "text").toLowerCase();
    return !["range", "checkbox", "radio", "button", "submit", "reset", "color", "file", "hidden"].includes(type);
  }
  return false;
}

function focusTerminalCursor() {
  if (isEditableFocusTarget(document.activeElement)) return;
  const term = activeTerm();
  if (!term) return;
  try {
    term.focus();
  } catch (_error) {
    // focus may be no-op before the terminal is fully open.
  }
}

function renderTerminal() {
  if (activeSourceSelection()) return;
  // While the human owns the pane, the screen is a live byte-streamed
  // terminal; captured-frame painting would fight the stream, so skip it.
  if (takeoverStream.active) return;
  // After takeover ends, all local paints are blocked until the fresh
  // server snapshot arrives; only that repaint may draw.
  if (takeoverRepaintPending) return;
  if (takeoverRepaintForceReset) {
    takeoverRepaintForceReset = false;
    termRowsState = null;
    state.terminalFrameKey = null;
  }
  const screens = eventsAtPlayback().filter((event) => event.kind === "screen");
  const replayReference = scoreReferenceAtPlayback();
  const replaySource = replayReference?.kind === "screen"
    ? state.timeline.find(
      (event) => event.kind === "screen" && event.event_id === replayReference.event_id,
    )
    : null;
  // Live mode always shows the newest captured frame: replaying the backlog
  // frame-by-frame made the terminal lag seconds behind the chat, which had
  // already rendered the agent's finished reply.
  const latest = replaySource || screens.at(-1);
  const fallback = state.followLive ? state.snapshot?.latest_screen : null;
  const source = latest || fallback;
  const terminal = source?.data || {};
  el.terminal_command.textContent = terminal.current_command || "shell";
  el.terminal_path.textContent = terminal.current_path
    ? terminal.current_path.split("/").slice(-2).join("/")
    : "workspace";
  const hasScreenContent = Boolean(source && typeof source.content === "string" && source.content.length);
  const sourceAnnotations = source ? scoreAnnotationsForSource(source) : [];
  const engine = ensureTerminalEngine();
  if (engine) {
    initializeTerminalCursor(engine);
    // Color-capable rendering through xterm.js. Frames are painted
    // incrementally: only rows that changed between two frames are rewritten,
    // so live typing no longer flashes the whole screen on every burst.
    const frameKey = hasScreenContent ? (source.event_id || `${source.seq ?? ""}`) : "";
    const width = Math.max(1, Number(terminal.pane_width) || 80);
    const fullRows = Math.max(1, Number(terminal.pane_height) || 24);
    // Fixed font size: when the panel is shorter than the captured frame,
    // show the bottom window of the frame (like a terminal scrolled to the
    // bottom) instead of shrinking the font. The cursor line stays visible.
    const panelHeight = el.terminal_screen.clientHeight || 0;
    const cellHeight = terminalCellHeight(engine);
    let fittingRows = fullRows;
    if (panelHeight > 0) {
      fittingRows = Math.floor((panelHeight - TERMINAL_BOTTOM_RESERVE_PX) / cellHeight);
      // Squeeze one extra row when it overflows by at most 2px, so the panel
      // is filled without an awkward empty strip under the prompt.
      if ((fittingRows + 1) * cellHeight - panelHeight <= 2) fittingRows += 1;
      fittingRows = Math.max(4, Math.min(fullRows, fittingRows));
    }
    const viewRows = Math.min(fullRows, fittingRows);
    const geometryChanged = engine.cols !== width || engine.rows !== viewRows;
    if (frameKey !== state.terminalFrameKey || geometryChanged) {
      state.terminalFrameKey = frameKey;
      lastTerminalPlainText = hasScreenContent ? source.content : "";
      lastTerminalAnsiText = typeof terminal.ansi === "string" ? terminal.ansi : "";
      lastTerminalFrame = hasScreenContent
        ? { event_id: source.event_id || "", content: source.content }
        : null;
      if (geometryChanged) engine.resize(width, viewRows);
      const ansi = typeof terminal.ansi === "string" ? terminal.ansi : "";
      const waiting = !ansi && !hasScreenContent;
      const cursorX = Number(terminal.cursor_x);
      const cursorY = Number(terminal.cursor_y);
      // The captured pane is top-anchored (blank rows pad the bottom). When
      // the panel is shorter than the pane, slide the visible window down
      // only once the cursor passes it — always cutting the top rows hid the
      // prompt and typed text while the shell was still near the top.
      let rowOffset = 0;
      if (viewRows < fullRows) {
        const anchorRow = Number.isFinite(cursorY) ? cursorY : fullRows - 1;
        rowOffset = Math.max(0, Math.min(fullRows - viewRows, anchorRow - viewRows + 1));
      }
      if (waiting) {
        termRowsState = null;
        engine.reset();
        engine.write("等待终端画面…");
      } else {
        let rows = buildTerminalRows(terminal.segments, source.content, fullRows);
        rows = rows.slice(rowOffset, rowOffset + viewRows);
        paintTerminalFrame(engine, rows, width, viewRows, false);
      }
      if (Number.isFinite(cursorX) && Number.isFinite(cursorY)) {
        const visibleRow = Math.min(viewRows, Math.max(1, cursorY - rowOffset + 1));
        engine.write(`\x1b[${visibleRow};${Math.min(cursorX + 1, width)}H`);
      }
    }
    if (state.viewportMode === "follow") engine.scrollToBottom();
  } else {
    // Legacy DOM text rendering (used when xterm.js did not load).
    const previousScrollTop = el.terminal_screen.scrollTop;
    el.terminal_screen.replaceChildren();
    if (hasScreenContent) {
      appendAnnotatedText(el.terminal_screen, source.content, sourceAnnotations);
    } else {
      el.terminal_screen.append(document.createTextNode("等待终端画面…"));
    }
    if (state.viewportMode === "follow") {
      revealTerminalCursor(terminal);
    } else {
      const maxScroll = Math.max(0, el.terminal_screen.scrollHeight - el.terminal_screen.clientHeight);
      el.terminal_screen.scrollTop = Math.min(previousScrollTop, maxScroll);
    }
  }
  el.terminal_screen.dataset.eventId = hasScreenContent ? source.event_id || "" : "";
  el.terminal_screen.classList.toggle(
    "reference-target",
    Boolean(
      (state.highlightReference?.kind === "screen" && source?.event_id === state.highlightReference.event_id)
      || (replayReference?.kind === "screen" && source?.event_id === replayReference.event_id)
    ),
  );
  el.terminal_score_status.replaceChildren();
  const terminalSummary = scoreSummary(sourceAnnotations);
  if (terminalSummary) el.terminal_score_status.append(...terminalSummary.childNodes);
  const changeKind = terminal.change_kind;
  el.typing_state.textContent = changeKind === "paste" ? `${agentName(state.snapshot?.meeting)} 正在粘贴` : (changeKind === "typing" ? `${agentName(state.snapshot?.meeting)} 输入中` : "");
  el.typing_state.hidden = !["typing", "paste"].includes(changeKind);
}

function renderViewportMode() {
  const isFollow = state.viewportMode === "follow";
  for (const [button, active] of [[el.free_scroll_button, !isFollow], [el.follow_scroll_button, isFollow]]) {
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
}

function setViewportMode(mode) {
  if (!["free", "follow"].includes(mode)) return;
  if (state.viewportMode === mode) {
    state.chatFollow = mode === "follow";
    renderChat();
    return;
  }
  state.viewportMode = mode;
  state.chatFollow = mode === "follow";
  renderViewportMode();
  renderChat();
  renderTerminal();
}

function revealTerminalCursor(terminal) {
  const cursorRow = Number(terminal.cursor_y);
  if (!Number.isFinite(cursorRow)) {
    el.terminal_screen.scrollTop = 0;
    return;
  }
  const style = getComputedStyle(el.terminal_screen);
  const lineHeight = Number.parseFloat(style.lineHeight);
  const paddingTop = Number.parseFloat(style.paddingTop) || 0;
  const paddingBottom = Number.parseFloat(style.paddingBottom) || 0;
  if (!Number.isFinite(lineHeight) || lineHeight <= 0) return;
  const cursorTop = paddingTop + Math.max(0, cursorRow) * lineHeight;
  const cursorBottom = cursorTop + lineHeight;
  const visibleTop = el.terminal_screen.scrollTop;
  const visibleBottom = visibleTop + el.terminal_screen.clientHeight;
  if (cursorTop < visibleTop) {
    el.terminal_screen.scrollTop = Math.max(0, cursorTop - paddingTop);
  } else if (cursorBottom > visibleBottom) {
    el.terminal_screen.scrollTop = Math.max(
      0,
      cursorBottom - el.terminal_screen.clientHeight + paddingBottom,
    );
  }
}

function scoreStatusForEvent(event) {
  if (!event) return "unscored";
  if (event.kind === "score") {
    return event.data?.tag === "competent" || event.data?.tag === "incompetent"
      ? event.data.tag
      : "unscored";
  }
  if (event.kind !== "chat" && event.kind !== "screen") return "unscored";
  const annotations = scoreAnnotationsForSource(event, state.timeline);
  if (!annotations.length) return "unscored";
  return annotations.reduce(
    (latest, annotation) => (annotation.seq >= latest.seq ? annotation : latest),
    annotations[0],
  ).tag;
}

function hidePlaybackHover() {
  state.playbackHoverIndex = null;
  el.playback_hover.hidden = true;
  el.playback_hover.removeAttribute("data-status");
  el.playback_slider.title = "移动鼠标查看评价状态";
}

function showPlaybackHover(event) {
  const max = state.timeline.length - 1;
  const clientX = Number(event.clientX);
  if (max < 0 || !Number.isFinite(clientX)) {
    hidePlaybackHover();
    return;
  }
  const sliderRect = el.playback_slider.getBoundingClientRect();
  const trackRect = el.playback_track.getBoundingClientRect();
  const inset = Math.min(7, sliderRect.width / 2);
  const usableWidth = Math.max(1, sliderRect.width - inset * 2);
  const position = Math.max(0, Math.min(usableWidth, clientX - sliderRect.left - inset));
  const index = Math.max(0, Math.min(max, Math.round((position / usableWidth) * max)));
  const status = scoreStatusForEvent(eventAt(index));
  const label = labels.score[status] || labels.score.unscored;

  state.playbackHoverIndex = index;
  el.playback_hover.textContent = label;
  el.playback_hover.dataset.status = status;
  el.playback_hover.hidden = false;
  const halfWidth = Math.min(el.playback_hover.offsetWidth / 2, trackRect.width / 2);
  const rawLeft = clientX - trackRect.left;
  const left = Math.max(halfWidth, Math.min(trackRect.width - halfWidth, rawLeft));
  el.playback_hover.style.left = `${left}px`;
  el.playback_slider.title = `${index + 1} / ${state.timeline.length} · ${label}`;
}

function renderPlayback() {
  const max = state.timeline.length - 1;
  const hasEvents = max >= 0;
  const value = hasEvents ? Math.max(0, state.followLive ? max : state.playbackIndex) : 0;
  el.playback_slider.disabled = !hasEvents;
  el.playback_slider.max = String(Math.max(0, max));
  el.playback_slider.min = "0";
  el.playback_slider.value = String(value);
  el.playback_position.textContent = !hasEvents
    ? "无事件"
    : (state.followLive ? `实时 · ${state.timeline.length}` : `${state.playbackIndex + 1} / ${state.timeline.length}`);
  el.playback_slider.setAttribute("aria-valuetext", el.playback_position.textContent);
  el.live_button.disabled = state.followLive;
  el.play_button.textContent = state.playing ? "❚❚" : "▶";
  el.playback_hover.hidden = true;
  hidePlaybackHover();
}

function renderAll() {
  if (activeSourceSelection()) return;
  renderMeetingList();
  if (!state.snapshot) {
    el.welcome.hidden = false;
    el.session_view.hidden = true;
    return;
  }
  el.welcome.hidden = true;
  el.session_view.hidden = false;
  renderHeader();
  renderViewportMode();
  renderChat();
  renderTerminal();
  renderPlayback();
}

function referenceKey(reference) {
  const region = reference?.region && typeof reference.region === "object" ? reference.region : {};
  return `${reference?.kind || ""}:${reference?.event_id || ""}:${region.start ?? ""}:${region.end ?? ""}`;
}

function setReferences(references) {
  const unique = [];
  const seen = new Set();
  for (const reference of Array.isArray(references) ? references : []) {
    if (!reference || typeof reference !== "object") continue;
    const key = referenceKey(reference);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(reference);
  }
  state.references = unique;
  state.reference = unique[0] || null;
  if (!el.reference_card) return;
  el.reference_card.hidden = unique.length === 0;
  el.reference_card.replaceChildren();
  unique.forEach((reference, index) => {
    const item = node("div", "reference-item");
    item.append(
      node("span", "reference-kind", reference.kind === "screen" ? "Screen 引用" : "Chat 引用"),
      node("span", "reference-excerpt", reference.excerpt || ""),
    );
    const remove = node("button", "icon-button", "×");
    remove.type = "button";
    remove.title = `移除第 ${index + 1} 个引用`;
    remove.setAttribute("aria-label", remove.title);
    remove.addEventListener("click", () => setReferences(state.references.filter((_item, itemIndex) => itemIndex !== index)));
    item.append(remove);
    el.reference_card.append(item);
  });
}

function addReference(reference) {
  if (!reference || typeof reference !== "object") return false;
  const key = referenceKey(reference);
  if (state.references.some((item) => referenceKey(item) === key)) return false;
  setReferences([...state.references, reference]);
  return true;
}

// Backwards-compatible helper for integrations that used the old singular API.
function setReference(reference) {
  setReferences(reference ? [reference] : []);
}

function sourceRootFromNode(value) {
  if (!value) return null;
  const element = value.nodeType === 1 ? value : value.parentElement || value.parentNode;
  if (!element) return null;
  if (terminalEngine) {
    // Under xterm.js the terminal has no text DOM to select or annotate;
    // evaluation of terminal text is re-added on top of xterm selection
    // later. Chat message text stays selectable.
    return element.closest?.(".message-content") || null;
  }
  return element?.closest?.(".message-content, #terminal-screen") || null;
}

function activeSourceSelection() {
  if (state.pointerSelecting || state.selectionTimer !== null || state.pendingSelection !== null) return true;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return false;
  try {
    const range = selection.getRangeAt(0);
    const startRoot = sourceRootFromNode(range.startContainer);
    const endRoot = sourceRootFromNode(range.endContainer);
    return Boolean(startRoot && startRoot === endRoot);
  } catch (_error) {
    return false;
  }
}

function codePointOffsetBefore(root, container, offset) {
  if (!root || !container || !root.contains(container)) return null;
  try {
    const before = document.createRange();
    before.selectNodeContents(root);
    before.setEnd(container, offset);
    return codePointLength(before.toString());
  } catch (_error) {
    return null;
  }
}

function textBoundary(root, offset) {
  const total = codePointLength(root?.textContent || "");
  let remaining = Math.max(0, Math.min(total, Number(offset) || 0));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let current = walker.nextNode();
  while (current) {
    const value = current.nodeValue || "";
    const length = codePointLength(value);
    if (remaining <= length) {
      const utf16Offset = codePointSlice(value, 0, remaining).length;
      return { node: current, offset: utf16Offset };
    }
    remaining -= length;
    current = walker.nextNode();
  }
  return { node: root, offset: root.childNodes.length };
}

function setKeyboardRange(root, anchor, focus) {
  if (!root) return;
  const total = codePointLength(root.textContent || "");
  const normalizedAnchor = Math.max(0, Math.min(total, Number(anchor) || 0));
  const normalizedFocus = Math.max(0, Math.min(total, Number(focus) || 0));
  const anchorBoundary = textBoundary(root, normalizedAnchor);
  const focusBoundary = textBoundary(root, normalizedFocus);
  const range = document.createRange();
  try {
    if (normalizedAnchor <= normalizedFocus) {
      range.setStart(anchorBoundary.node, anchorBoundary.offset);
      range.setEnd(focusBoundary.node, focusBoundary.offset);
    } else {
      range.setStart(focusBoundary.node, focusBoundary.offset);
      range.setEnd(anchorBoundary.node, anchorBoundary.offset);
    }
    const selection = window.getSelection();
    if (!selection || typeof selection.removeAllRanges !== "function" || typeof selection.addRange !== "function") return;
    state.keyboardSelection = { root, anchor: normalizedAnchor, focus: normalizedFocus };
    selection.removeAllRanges();
    selection.addRange(range);
  } catch (_error) {
    state.keyboardSelection = null;
  }
}

function handleSourceFocus(event) {
  const root = sourceRootFromNode(event.target);
  if (!root) return;
  const selection = window.getSelection();
  let offset = 0;
  if (selection?.rangeCount === 1) {
    const range = selection.getRangeAt(0);
    if (range.collapsed && root.contains(range.startContainer)) {
      offset = codePointOffsetBefore(root, range.startContainer, range.startOffset) ?? 0;
    } else {
      const selected = selectionOffsets(root, selection);
      if (selected) {
        state.keyboardSelection = { root, anchor: selected.start, focus: selected.end };
        return;
      }
    }
  }
  state.keyboardSelection = { root, anchor: offset, focus: offset };
}

function handleKeyboardSelection(event) {
  const root = sourceRootFromNode(event.target);
  if (!root || event.isComposing) return;
  const total = codePointLength(root.textContent || "");
  const current = state.keyboardSelection?.root === root
    ? state.keyboardSelection
    : { root, anchor: 0, focus: 0 };
  const key = event.key;
  if ((event.metaKey || event.ctrlKey) && key.toLowerCase() === "a") {
    event.preventDefault();
    setKeyboardRange(root, 0, total);
    return;
  }
  const directional = key === "ArrowLeft" || key === "ArrowRight";
  const boundary = key === "Home" || key === "End";
  if (!directional && !boundary) return;
  const minimum = boundary ? (key === "Home" ? 0 : total) : Math.max(0, Math.min(total, current.focus + (key === "ArrowLeft" ? -1 : 1)));
  if (event.shiftKey) {
    event.preventDefault();
    setKeyboardRange(root, current.anchor, minimum);
    return;
  }
  event.preventDefault();
  if (directional && current.anchor !== current.focus) {
    const collapsed = key === "ArrowLeft" ? Math.min(current.anchor, current.focus) : Math.max(current.anchor, current.focus);
    setKeyboardRange(root, collapsed, collapsed);
  } else {
    setKeyboardRange(root, minimum, minimum);
  }
}

function selectionOffsets(root, selection) {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
  try {
    const range = selection.getRangeAt(0);
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
    const content = root.textContent || "";
    const start = textOffsetFromPoint(root, range.startContainer, range.startOffset);
    const end = textOffsetFromPoint(root, range.endContainer, range.endOffset);
    if (start === null || end === null || start < 0 || end > codePointLength(content) || end <= start) return null;
    const excerpt = codePointSlice(content, start, end);
    if (!excerpt.trim()) return null;
    return { start, end, excerpt };
  } catch (_error) {
    return null;
  }
}

function terminalSelectionCandidate() {
  try {
    const term = terminalEngine ? terminalEngine.term : null;
    if (!term || !lastTerminalFrame || !lastTerminalFrame.event_id) return null;
    const text = term.getSelection();
    if (!text || !text.trim()) return null;
    const position = typeof term.getSelectionPosition === "function" ? term.getSelectionPosition() : null;
    if (!position || !position.start || !position.end) return null;
    const rows = String(lastTerminalFrame.content ?? "").split("\n");
    const rowOffset = Math.max(0, rows.length - term.rows);
    const charOffsetAt = (row, col) => {
      const safeRow = Math.max(0, Math.min(rows.length - 1, row + rowOffset));
      const prefixLength = rows.slice(0, safeRow).reduce((total, line) => total + line.length + 1, 0);
      const column = Math.max(0, Math.min(col, rows[safeRow].length));
      return prefixLength + column;
    };
    const rawStart = charOffsetAt(position.start.y, position.start.x);
    const rawEnd = charOffsetAt(position.end.y, position.end.x);
    const start = Math.max(0, Math.min(rawStart, rawEnd, lastTerminalFrame.content.length));
    const end = Math.max(0, Math.min(Math.max(rawStart, rawEnd), lastTerminalFrame.content.length));
    if (end <= start) return null;
    const excerpt = lastTerminalFrame.content.slice(start, end);
    if (!excerpt.trim()) return null;
    const point = lastTerminalSelectionPoint
      || { x: 0, y: 0 };
    return {
      kind: "screen",
      event_id: lastTerminalFrame.event_id,
      start,
      end,
      excerpt,
      rect: { left: point.x, top: point.y, width: 0, height: 0 },
    };
  } catch (_error) {
    return null;
  }
}

function selectionCandidate() {
  try {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    const startRoot = sourceRootFromNode(range.startContainer);
    const endRoot = sourceRootFromNode(range.endContainer);
    let root = startRoot || endRoot;
    if (!root) return null;

    let clampToStart = false;
    let clampToEnd = false;
    if (startRoot && endRoot && startRoot !== endRoot) {
      // Triple-click can spill from one message into the next message or into
      // the composer. If the selection starts in an earlier source element and
      // ends outside it, clamp to that source element's end. If it starts
      // before the source and ends inside it, clamp to that source's start.
      const startBeforeEnd = Boolean(
        startRoot.compareDocumentPosition(endRoot) & Node.DOCUMENT_POSITION_FOLLOWING,
      );
      root = startBeforeEnd ? startRoot : endRoot;
      clampToEnd = startBeforeEnd;
      clampToStart = !startBeforeEnd;
    } else if (startRoot && !endRoot) {
      clampToEnd = true;
    } else if (!startRoot && endRoot) {
      clampToStart = true;
    }

    const source = sourceEventForRoot(root);
    if (!source) return null;
    const content = root.textContent || "";
    const length = codePointLength(content);
    let start;
    let end;
    if (!clampToStart && !clampToEnd && startRoot && endRoot) {
      const offsets = selectionOffsets(root, selection);
      if (!offsets) return null;
      start = offsets.start;
      end = offsets.end;
    } else if (clampToEnd) {
      start = textOffsetFromPoint(root, range.startContainer, range.startOffset);
      end = length;
    } else {
      start = 0;
      end = textOffsetFromPoint(root, range.endContainer, range.endOffset);
    }
    if (start === null || end === null || start < 0 || end > length || end <= start) return null;
    const excerpt = codePointSlice(content, start, end);
    if (!excerpt.trim()) return null;
    const rect = range.getBoundingClientRect();
    return { start, end, excerpt, kind: source.kind, event_id: source.event_id, root, rect };
  } catch (_error) {
    return null;
  }
}

function clearBrowserSelection() {
  const selection = window.getSelection();
  if (selection && selection.rangeCount && typeof selection.removeAllRanges === "function") {
    selection.removeAllRanges();
  }
  state.keyboardSelection = null;
}

function hideSelectionMenu(clearPending = true) {
  if (!el.selection_menu) return;
  el.selection_menu.hidden = true;
  if (clearPending && !state.selectionBusy) state.pendingSelection = null;
}

function positionSelectionMenu(candidate) {
  if (!candidate || !el.selection_menu) return;
  const rect = candidate.rect;
  const menu = el.selection_menu;
  const menuRect = menu.getBoundingClientRect();
  const gutter = 8;
  const left = Math.max(gutter, Math.min(window.innerWidth - menuRect.width - gutter, rect.left + rect.width / 2 - menuRect.width / 2));
  let top = rect.top - menuRect.height - 10;
  if (top < gutter) top = Math.min(window.innerHeight - menuRect.height - gutter, rect.bottom + 10);
  menu.style.left = `${Math.max(gutter, left)}px`;
  menu.style.top = `${Math.max(gutter, top)}px`;
}

function selectionIsHumanChat(candidate) {
  if (!candidate || candidate.kind !== "chat") return false;
  const source = state.timeline.find((event) => event.event_id === candidate.event_id);
  return source?.actor === "human";
}

function showSelectionMenu(candidate) {
  if (!candidate || !el.selection_menu || state.selectionBusy || state.evaluationSelection) return;
  state.pendingSelection = candidate;
  const terminal = ["completed", "failed", "interrupted"].includes(state.snapshot?.meeting?.status);
  const humanChat = selectionIsHumanChat(candidate);
  el.selection_menu.querySelectorAll("[data-selection-action]").forEach((button) => {
    if (button.dataset.selectionAction === "add-to-conversation") {
      button.disabled = terminal;
      button.title = terminal ? "会话已结束，无法继续发消息" : "";
    } else if (button.dataset.selectionAction === "evaluate") {
      button.disabled = humanChat;
      button.title = humanChat ? "人类消息不能被评价" : "";
    }
  });
  el.selection_menu.hidden = false;
  requestAnimationFrame(() => positionSelectionMenu(candidate));
}

function scheduleSelectionMenu(clickCount = 1) {
  // During live takeover the terminal shows a byte stream, not a captured
  // frame, so quoting "the screen" from a selection is meaningless there;
  // suppress the reference menu and keep plain copy working.
  if (takeoverStream.active) return;
  // A double-click may be the first two clicks of a triple-click. Wait a bit
  // after double-click so the third click can replace the word selection with
  // the paragraph selection instead of being blocked by an early menu.
  const delay = clickCount >= 3 ? 60 : (clickCount === 2 ? 350 : 45);
  // selectionchange events do not know the click count. If a pointerup has
  // already scheduled a longer double-click delay, do not let the later
  // selectionchange shorten it and pop the menu before a possible third click.
  if (state.selectionTimer !== null && delay < state.selectionMenuDelay) return;
  clearTimeout(state.selectionTimer);
  state.selectionMenuDelay = delay;
  // While the primary button is held the user is still dragging a selection;
  // the menu must only appear after release (pointerup/mouseup/touchend).
  if (state.pointerSelecting) {
    hideSelectionMenu();
    return;
  }
  state.selectionTimer = setTimeout(() => {
    state.selectionTimer = null;
    state.selectionMenuDelay = 0;
    if (state.selectionBusy || state.pointerSelecting) return;
    const terminalCandidate = terminalEngine ? terminalSelectionCandidate() : null;
    const candidate = terminalCandidate || selectionCandidate();
    if (candidate) showSelectionMenu(candidate);
    else hideSelectionMenu();
  }, delay);
}

function openEvaluationPanel(candidate) {
  if (!candidate || !el.evaluation_panel) return;
  state.evaluationSelection = candidate;
  state.pendingSelection = null;
  el.evaluation_error.hidden = true;
  el.evaluation_excerpt.textContent = candidate.excerpt || "";
  el.evaluation_form.querySelectorAll('input[name="evaluation-tag"]').forEach((input) => {
    input.checked = false;
    input.disabled = false;
  });
  el.evaluation_comment.value = "";
  el.message_form.hidden = true;
  el.evaluation_panel.hidden = false;
  clearBrowserSelection();
  hideSelectionMenu(false);
  requestAnimationFrame(() => el.evaluation_form.querySelector('input[name="evaluation-tag"]')?.focus());
}

function closeEvaluationPanel() {
  state.evaluationSelection = null;
  state.evaluationBusy = false;
  if (el.evaluation_form) {
    el.evaluation_form.querySelectorAll('input[name="evaluation-tag"]').forEach((input) => {
      input.checked = false;
      input.disabled = false;
    });
  }
  if (el.evaluation_comment) el.evaluation_comment.value = "";
  if (el.evaluation_error) el.evaluation_error.hidden = true;
  if (el.evaluation_panel) el.evaluation_panel.hidden = true;
  if (el.message_form) el.message_form.hidden = false;
}

function selectedDeliveryMode() {
  return el.delivery_mode?.querySelector('input[name="delivery-choice"]:checked')?.value || "next_action";
}

async function submitEvaluation(event) {
  event?.preventDefault();
  const selected = state.evaluationSelection;
  if (!selected || !state.selectedId || state.evaluationBusy) return;
  const tag = el.evaluation_form.querySelector('input[name="evaluation-tag"]:checked')?.value;
  if (!tag) {
    el.evaluation_error.textContent = "请选择胜任或不胜任";
    el.evaluation_error.hidden = false;
    return;
  }
  const comment = el.evaluation_comment.value.trim();
  state.evaluationBusy = true;
  el.evaluation_submit_button.disabled = true;
  el.evaluation_back_button.disabled = true;
  el.evaluation_cancel_button.disabled = true;
  el.evaluation_error.hidden = true;
  try {
    const response = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/scores`, {
      method: "POST",
      body: JSON.stringify({
        tag,
        comment: comment || null,
        reference: {
          kind: selected.kind,
          event_id: selected.event_id,
          region: { start: selected.start, end: selected.end },
        },
      }),
    });
    const scoreEvent = response?.event;
    if (!state.followLive && scoreEvent?.event_id) state.scoreOverlayIds.add(scoreEvent.event_id);
    if (scoreEvent) mergeTimelineEvents([scoreEvent]);
    closeEvaluationPanel();
    clearBrowserSelection();
    renderAll();
    try {
      await refreshSelected();
      toast(`已记录${labels.score[tag]}评价`);
    } catch (refreshError) {
      toast(`评价已保存，页面刷新失败：${refreshError.message}`);
    }
  } catch (error) {
    el.evaluation_error.textContent = error.message;
    el.evaluation_error.hidden = false;
  } finally {
    state.evaluationBusy = false;
    el.evaluation_submit_button.disabled = false;
    el.evaluation_back_button.disabled = false;
    el.evaluation_cancel_button.disabled = false;
  }
}

async function handleSelectionAction(action) {
  const selected = state.pendingSelection;
  if (!selected || state.selectionBusy || state.evaluationSelection) return;
  if (action === "evaluate") {
    if (selectionIsHumanChat(selected)) {
      toast("人类消息不能被评价");
      return;
    }
    openEvaluationPanel(selected);
    return;
  }
  if (action === "add-to-conversation") {
    if (["completed", "failed", "interrupted"].includes(state.snapshot?.meeting?.status)) return;
    const added = addReference({
      kind: selected.kind,
      event_id: selected.event_id,
      region: { start: selected.start, end: selected.end },
      excerpt: selected.excerpt,
    });
    hideSelectionMenu();
    clearBrowserSelection();
    el.message_input.focus();
    toast(added ? `已添加引用（共 ${state.references.length} 个）` : "这个引用已经添加");
    return;
  }
}

async function loadHealth() {
  const health = await api("/api/health");
  state.health = health;
  const backendEntries = Object.values(health.backends || {});
  state.healthy = Boolean(health.core_ok && backendEntries.some(usableBackendEntry));
  el.health_dot.classList.toggle("ok", Boolean(health.ok));
  const failedChecks = (health.checks || []).filter((item) => !item.ok);
  const failed = failedChecks.map((item) => item.detail);
  const visibleFailures = failedChecks.map((item) => {
    if (item.name === "model-api") return "请配置 model_api/config.toml 与 auth.json";
    if (item.name === "tmux") return "未找到 tmux";
    if (item.name === "vim") return "未找到 Vim";
    if (item.name === "codex-app-server") return `Codex 后端不可用：${item.detail}`;
    if (item.name === "claude-code") return `Claude Code 后端不可用：${item.detail}`;
    return "当前 Python 版本不受支持";
  });
  el.health_dot.title = health.ok ? "默认运行环境就绪" : failed.join("\n");
  el.health_message.textContent = visibleFailures.join("；");
  el.health_message.hidden = state.healthy;
  el.new_button.disabled = !state.healthy;
  el.welcome_new_button.disabled = !state.healthy;
  syncBackendSelectors();
}

function backendEntry(backend, provider) {
  return state.health?.backends?.[`${backend}/${provider}`] || null;
}

// codex-app-server authenticates via the local CLI login, so it stays
// selectable even when its probe fails — the dialog then shows the real
// reason (e.g. "Codex CLI is not logged in") instead of hiding the option.
function usableBackendEntry(item) {
  return item.ok || item.needs_api_key || item.agent_backend === "codex-app-server";
}

function syncBackendSelectors(preferCurrent = true) {
  if (!el.backend_select || !el.provider_select) return;
  const entries = Object.values(state.health?.backends || {});
  const usable = usableBackendEntry;
  for (const option of el.backend_select.options) {
    option.disabled = !entries.some((item) => item.agent_backend === option.value && usable(item));
  }
  let backend = preferCurrent ? el.backend_select.value : state.health?.agent_backend;
  if (!entries.some((item) => item.agent_backend === backend && usable(item))) {
    backend = (entries.find((item) => item.ok) || entries.find(usable))?.agent_backend || backend;
  }
  if (backend) {
    el.backend_select.value = backend;
  }
  for (const option of el.provider_select.options) {
    option.disabled = !entries.some((item) => item.agent_backend === backend && item.agent_provider === option.value && usable(item));
  }
  let provider = preferCurrent ? el.provider_select.value : state.health?.agent_provider;
  const entry = entries.find((item) => item.agent_backend === backend && item.agent_provider === provider && usable(item))
    || entries.find((item) => item.agent_backend === backend && item.ok)
    || entries.find((item) => item.agent_backend === backend && usable(item));
  if (entry) {
    el.provider_select.value = entry.agent_provider;
    el.model_input.placeholder = `默认 ${entry.default_model || "由服务端决定"}`;
  }
  syncCliDefaultModel();
  updateCredentialFields();
}

function syncCliDefaultModel() {
  if (!el.cli_default_model_row) return;
  const isCodex = el.backend_select.value === "codex-app-server";
  el.cli_default_model_row.hidden = !isCodex;
  const cliDefault = isCodex && el.cli_default_model.checked;
  el.model_input.disabled = cliDefault;
  if (cliDefault) {
    el.model_input.placeholder = "由 Codex CLI 决定";
  } else {
    const entry = backendEntry(el.backend_select.value, el.provider_select.value);
    el.model_input.placeholder = `默认 ${entry?.default_model || "由服务端决定"}`;
  }
}

function updateCredentialFields() {
  if (!el.api_key_input) return;
  const entry = backendEntry(el.backend_select.value, el.provider_select.value);
  const provider = el.provider_select.value;
  const isCodex = el.backend_select.value === "codex-app-server";
  if (el.api_credential_fields) el.api_credential_fields.hidden = isCodex;
  if (isCodex) {
    // codex authenticates through the local CLI login; surface the probe's
    // real failure reason instead of asking for an API key.
    el.api_key_hint.textContent = entry && !entry.ok
      ? `Codex 后端尚未就绪：${entry.detail || "未知原因"}。如未登录，请在终端运行 codex login。`
      : "该组合使用本地 Codex CLI 登录（含 ChatGPT 订阅），无需 API Key。";
    el.api_key_hint.hidden = false;
    return;
  }
  const authName = entry?.auth_key_name || (provider === "deepseek" ? "DEEPSEEK_API_KEY" : "OPENAI_API_KEY");
  el.api_key_name.textContent = authName;
  el.api_key_input.placeholder = entry?.has_api_key ? "已保存，留空沿用" : "尚未配置，请填写";
  el.base_url_input.placeholder = entry?.base_url ? `默认 ${entry.base_url}` : "留空使用默认地址";
  if (entry && !entry.ok) {
    el.api_key_hint.textContent = entry.needs_api_key
      ? `该组合还需要 ${authName}，填写后会保存到 model_api/auth.json（仅本机）。`
      : `该组合尚未就绪：${entry.detail || "未知原因"}`;
    el.api_key_hint.hidden = false;
  } else {
    el.api_key_hint.hidden = true;
  }
}

async function loadMeetings() {
  const payload = await api("/api/meetings");
  state.meetings = payload.meetings || [];
  state.meetingsRefreshedAt = Date.now();
  renderMeetingList();
}

async function selectMeeting(meetingId) {
  // Selecting a session from the (overlay) sidebar should get out of the way
  // and give the workspace the full width. Static sidebar layouts (narrow
  // screens) keep it visible.
  const appShell = document.querySelector(".app-shell");
  const sideBar = document.querySelector(".sidebar");
  if (
    appShell
    && sideBar
    && getComputedStyle(sideBar).position === "fixed"
  ) {
    appShell.classList.remove("sidebar-open");
  }
  stopLiveScreenQueue();
  hideSelectionMenu();
  hidePlaybackHover();
  clearBrowserSelection();
  closeEvaluationPanel();
  setReferences([]);
  state.scoreOverlayIds.clear();
  state.selectedId = meetingId;
  state.snapshotEtag = "";
  state.eventsEtag = "";
  state.eventsEtagCursor = -1;
  const [snapshot, eventPayload] = await Promise.all([
    api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`),
    api(`/api/meetings/${encodeURIComponent(meetingId)}/events?after=0`),
  ]);
  if (meetingId !== state.selectedId) return;
  state.snapshot = snapshot;
  state.timeline = eventPayload.events || [];
  state.cursor = eventPayload.cursor || 0;
  state.playbackFreezeAt = 0;
  state.followLive = true;
  state.chatFollow = true;
  state.playbackIndex = state.timeline.length - 1;
  renderAll();
  schedulePoll();
  // Opening a session lands in live view: blink the terminal cursor.
  focusTerminalCursor();
}

async function refreshSelected() {
  const meetingId = state.selectedId;
  if (!meetingId) return;
  const cursor = state.cursor;
  const eventsEtag = cursor === state.eventsEtagCursor ? state.eventsEtag : "";
  const [snapshotResult, eventsResult] = await Promise.all([
    apiConditional(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`, state.snapshotEtag),
    apiConditional(`/api/meetings/${encodeURIComponent(meetingId)}/events?after=${cursor}`, eventsEtag),
  ]);
  if (meetingId !== state.selectedId) return;
  let changed = false;
  let additions = [];
  if (!snapshotResult.notModified) {
    state.snapshot = snapshotResult.payload;
    state.snapshotEtag = snapshotResult.etag;
    changed = true;
  }
  if (!eventsResult.notModified) {
    state.eventsEtag = eventsResult.etag;
    state.eventsEtagCursor = cursor;
    additions = mergeTimelineEvents(eventsResult.payload.events || []);
    if (additions.length) enqueueLiveScreens(additions);
    state.cursor = Math.max(Number(eventsResult.payload.cursor) || 0, state.cursor);
    if (additions.length) changed = true;
    // If new frames arrive while the timeline is parked on an old frame
    // (e.g. from an earlier scrub) and the user has not touched playback
    // recently, jump back to live so the terminal never lags the chat.
    if (
      additions.length
      && !state.playing
      && !state.followLive
      && Date.now() - (state.playbackFreezeAt || 0) > 4000
    ) {
      state.followLive = true;
      state.chatFollow = true;
      state.playbackIndex = state.timeline.length - 1;
    }
  }
  if (state.followLive) state.playbackIndex = state.timeline.length - 1;
  if (!changed) return;
  // Re-rendering replaces the chat/terminal DOM and would collapse an
  // in-progress text selection — the app's core scoring interaction.
  if (state.pointerSelecting || state.pendingSelection || state.evaluationSelection) return;
  if (hasLiveSelection()) return; // 用户正在选中/复制：本 tick 放弃重绘
  renderAll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!state.selectedId) return;
  const active = state.snapshot && !["completed", "failed", "interrupted"].includes(state.snapshot.meeting.status)
    && !["api_error", "cycle_timeout", "waiting_for_user", "completion_proposed", "human_control"].includes(state.snapshot.meeting.phase);
  state.pollTimer = setTimeout(async () => {
    try {
      await refreshSelected();
      // The sidebar list is expensive server-side; refresh it on its own
      // slow cadence instead of on every poll tick.
      if (active && Date.now() - (state.meetingsRefreshedAt || 0) >= 10000) {
        await loadMeetings();
      }
    } catch (error) { toast(error.message); }
    schedulePoll();
  }, active ? 500 : 1000);
}

async function postMeetingAction(suffix, body = {}) {
  if (!state.selectedId) return;
  await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/${suffix}`, { method: "POST", body: JSON.stringify(body) });
  await refreshSelected();
  await loadMeetings();
}

function openNewDialog() {
  el.new_error.hidden = true;
  el.api_key_input.value = "";
  el.base_url_input.value = "";
  syncBackendSelectors(false);
  el.new_dialog.showModal();
  el.initial_message.focus();
}

async function loadSettings() {
  const payload = await api("/api/settings");
  state.judgeName = typeof payload.judge_name === "string" && payload.judge_name.trim()
    ? payload.judge_name.trim()
    : "human";
  el.judge_name_input.value = state.judgeName;
}

function openSettingsDialog() {
  el.settings_error.hidden = true;
  el.judge_name_input.value = state.judgeName;
  el.settings_dialog.showModal();
  el.judge_name_input.focus();
  el.judge_name_input.select();
}

function bind() {
  el.new_button.addEventListener("click", openNewDialog);
  el.welcome_new_button.addEventListener("click", openNewDialog);
  const railNew = document.getElementById("rail-new-button");
  const railMore = document.getElementById("rail-more-button");
  const appShell = document.querySelector(".app-shell");
  if (railNew) railNew.addEventListener("click", openNewDialog);
  if (railMore && appShell) {
    railMore.addEventListener("click", () => appShell.classList.toggle("sidebar-open"));
  }
  document.querySelector(".workspace-main")?.addEventListener("pointerdown", () => {
    appShell?.classList.remove("sidebar-open");
  });
  const sessionMore = document.getElementById("session-more-button");
  const sessionView = document.getElementById("session-view");
  if (sessionMore && sessionView) {
    sessionMore.addEventListener("click", () => {
      const open = sessionView.classList.toggle("details-open");
      sessionMore.setAttribute("aria-expanded", String(open));
      sessionMore.title = open ? "收起会话详情" : "展开会话详情";
    });
  }
  el.settings_button.addEventListener("click", openSettingsDialog);
  el.close_dialog.addEventListener("click", () => el.new_dialog.close());
  el.cancel_new.addEventListener("click", () => el.new_dialog.close());
  el.backend_select.addEventListener("change", () => syncBackendSelectors(true));
  el.provider_select.addEventListener("change", () => syncBackendSelectors(true));
  el.cli_default_model?.addEventListener("change", () => syncCliDefaultModel());
  el.evaluation_form.addEventListener("submit", submitEvaluation);
  el.evaluation_back_button.addEventListener("click", closeEvaluationPanel);
  el.evaluation_cancel_button.addEventListener("click", closeEvaluationPanel);
  el.close_settings.addEventListener("click", () => el.settings_dialog.close());
  el.cancel_settings.addEventListener("click", () => el.settings_dialog.close());
  el.settings_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    el.save_settings.disabled = true;
    el.settings_error.hidden = true;
    try {
      const payload = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ judge_name: el.judge_name_input.value }),
      });
      state.judgeName = payload.judge_name;
      el.judge_name_input.value = state.judgeName;
      el.settings_dialog.close();
    } catch (error) {
      el.settings_error.textContent = error.message;
      el.settings_error.hidden = false;
    } finally {
      el.save_settings.disabled = false;
    }
  });
  el.new_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    el.start_button.disabled = true;
    el.new_error.hidden = true;
    try {
      const apiKey = el.api_key_input.value.trim();
      const baseUrl = el.base_url_input.value.trim();
      if (apiKey || baseUrl) {
        const credentials = { provider: el.provider_select.value };
        if (apiKey) credentials.api_key = apiKey;
        if (baseUrl) credentials.base_url = baseUrl;
        await api("/api/credentials", { method: "POST", body: JSON.stringify(credentials) });
        el.api_key_input.value = "";
        el.base_url_input.value = "";
        await loadHealth();
      }
      const typingProfile = document.querySelector('input[name="typing-profile"]:checked')?.value || "balanced";
      // Empty string = follow the Codex CLI's own default model; null = the
      // server config's default.
      const cliDefault = el.backend_select.value === "codex-app-server" && el.cli_default_model?.checked;
      const response = await api("/api/meetings", {
        method: "POST",
        body: JSON.stringify({
          message: el.initial_message.value,
          agent_backend: el.backend_select.value,
          agent_provider: el.provider_select.value,
          model: cliDefault ? "" : (el.model_input.value.trim() || null),
          typing_profile: typingProfile,
          ...(el.effort_select.value ? { reasoning_effort: el.effort_select.value } : {}),
        }),
      });
      el.new_dialog.close();
      el.initial_message.value = "";
      await loadMeetings();
      await selectMeeting(response.meeting_id);
    } catch (error) {
      el.new_error.textContent = error.message;
      el.new_error.hidden = false;
    } finally { el.start_button.disabled = false; }
  });
  el.message_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = el.message_input.value.trim();
    if (!message) return;
    el.send_message_button.disabled = true;
    // Sending a message is a live action: leave playback/free-scroll mode
    // so the user's own text and the agent's next response are visible.
    state.followLive = true;
    state.chatFollow = true;
    state.playbackIndex = state.timeline.length - 1;
    try {
      const payload = { message, delivery: selectedDeliveryMode() };
      if (state.references.length) payload.references = state.references;
      await postMeetingAction("messages", payload);
      el.message_input.value = "";
      setReferences([]);
    }
    catch (error) { toast(error.message); }
    finally { el.send_message_button.disabled = false; }
  });
  // Enter sends the message immediately; Shift+Enter / ⌘+Enter insert a real
  // newline. A confirming Enter while an IME composition is active must not
  // send.
  el.message_input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    if (event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) return;
    event.preventDefault();
    el.message_form.requestSubmit();
  });
  el.refresh_button.addEventListener("click", () => refreshSelected().catch((error) => toast(error.message)));
  el.effort_live_select?.addEventListener("change", async () => {
    try {
      await postMeetingAction("effort", { reasoning_effort: el.effort_live_select.value || null });
      toast("推理力度已更新，下一个工作周期生效");
    } catch (error) { toast(error.message); }
  });
  el.acquire_button.addEventListener("click", () => postMeetingAction("control/acquire").catch((error) => toast(error.message)));
  el.release_button.addEventListener("click", () => postMeetingAction("control/release").catch((error) => toast(error.message)));
  el.terminal_copy_button?.addEventListener("click", () => copyTextToClipboard(lastTerminalPlainText));
  el.terminal_copy_ansi_button?.addEventListener("click", () => copyTextToClipboard(lastTerminalAnsiText || lastTerminalPlainText));
  // Refit the terminal when the window/panel size changes (DevTools dock,
  // pane resize), so rows are never clipped and the cursor stays visible.
  window.addEventListener("resize", () => refitTerminal());
  el.resume_button.addEventListener("click", () => postMeetingAction("agent/resume").catch((error) => toast(error.message)));
  el.complete_button.addEventListener("click", () => { if (confirm("结束会话并保留 tmux 工作区？")) postMeetingAction("complete").catch((error) => toast(error.message)); });
  el.stop_button.addEventListener("click", () => { if (confirm("中止会话并清理 tmux 工作区？")) postMeetingAction("stop").catch((error) => toast(error.message)); });
  el.cleanup_button.addEventListener("click", () => postMeetingAction("cleanup").catch((error) => toast(error.message)));
  document.querySelectorAll("[data-chat-filter]").forEach((button) => button.addEventListener("click", () => {
    state.chatFilter = button.dataset.chatFilter;
    document.querySelectorAll("[data-chat-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderChat();
  }));
  for (const container of [el.chat_list, el.terminal_screen]) {
    container.addEventListener("pointerdown", (event) => {
      if (event.button === 0) state.pointerSelecting = true;
    });
    container.addEventListener("pointerup", (event) => scheduleSelectionMenu(event.detail || 1));
    container.addEventListener("mouseup", (event) => scheduleSelectionMenu(event.detail || 1));
    container.addEventListener("touchend", () => setTimeout(scheduleSelectionMenu, 70), { passive: true });
    container.addEventListener("focusin", handleSourceFocus);
    container.addEventListener("keydown", handleKeyboardSelection);
  }
  // Release anywhere (even outside the containers) ends the drag; re-evaluate
  // the selection once so the menu appears exactly at mouse release.
  document.addEventListener("pointerup", (event) => {
    if (!state.pointerSelecting) return;
    state.pointerSelecting = false;
    scheduleSelectionMenu(event.detail || 1);
  });
  document.addEventListener("pointercancel", () => { state.pointerSelecting = false; });
  document.addEventListener("selectionchange", scheduleSelectionMenu);
  document.addEventListener("pointerdown", (event) => {
    if (el.selection_menu.contains(event.target)) return;
    hideSelectionMenu();
    // Do not clear the browser selection when the user is clicking inside a
    // selectable source. Double/triple-click selection depends on the browser
    // preserving and extending the previous selection across rapid clicks.
    if (!sourceRootFromNode(event.target)) {
      clearBrowserSelection();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (state.evaluationSelection && !state.evaluationBusy) {
        closeEvaluationPanel();
      }
      hideSelectionMenu();
      clearBrowserSelection();
      state.keyboardSelection = null;
    }
  });
  // While the user is watching the terminal (live or takeover), clicking
  // anywhere outside it -- including non-focusable surfaces like chat blank
  // space, where no focus event ever fires -- moves focus to <body> and stops
  // the cursor blink. After such a click, hand focus straight back to the
  // Selectable-text surfaces must never lose focus to the terminal on
  // pointerdown: the focus grab below is meant to restart the cursor blink,
  // but stealing focus out from under a mousedown on chat text (or the
  // header identity) also cancels native text selection before it can start
  // (drag, double-click and Shift+arrows all die; copy returns nothing).
  function isTextSelectionSurface(node) {
    const selectors = [".message-content", ".session-identity", "#meeting-title",
                       ".presence-strip", ".evaluation-excerpt"];
    for (const selector of selectors) {
      if (node && node.closest && node.closest(selector)) return true;
    }
    return false;
  }
  // terminal; editable fields (chat composer, dialogs) are never touched, and
  // playback scrubbing (followLive false) leaves focus alone.
  document.addEventListener("pointerdown", (event) => {
    if (!(takeoverStream.active || state.followLive)) return;
    const target = event.target;
    if (el.terminal_screen && (target === el.terminal_screen || el.terminal_screen.contains(target))) return;
    if (isEditableFocusTarget(target)) return;
    if (isTextSelectionSurface(target)) return;
    setTimeout(focusTerminalCursor, 0);
  }, true);
  el.selection_menu.addEventListener("pointerdown", (event) => event.stopPropagation());
  el.selection_menu.querySelectorAll("[data-selection-action]").forEach((button) => {
    button.addEventListener("click", () => handleSelectionAction(button.dataset.selectionAction));
  });
  el.chat_list.addEventListener("scroll", () => {
    if (state.renderingChat) return;
    if (state.followLive) {
      // Live transcript: pinned means near the newest message at the bottom.
      const remaining = el.chat_list.scrollHeight - el.chat_list.clientHeight - el.chat_list.scrollTop;
      state.chatFollow = remaining <= 12;
    } else {
      // Playback: only a genuine user scroll pauses following the playback
      // cursor; pressing play, scrubbing the timeline, or the follow button
      // re-pins. The scroll renderChat applies itself arrives here as an
      // async scroll event too, so it must not count as user intent.
      const applied = state.chatScrollApplied;
      if (applied === null || Math.abs(el.chat_list.scrollTop - applied) > 1) {
        state.chatFollow = false;
      }
    }
  }, { passive: true });
  el.free_scroll_button.addEventListener("click", () => setViewportMode("free"));
  el.follow_scroll_button.addEventListener("click", () => setViewportMode("follow"));
  el.playback_slider.addEventListener("mousemove", showPlaybackHover);
  el.playback_slider.addEventListener("mouseleave", hidePlaybackHover);
  el.playback_slider.addEventListener("pointercancel", hidePlaybackHover);
  el.playback_slider.addEventListener("input", () => {
    state.scoreOverlayIds.clear();
    hidePlaybackHover();
    markPlaybackFrozen();
    state.followLive = false;
    state.chatFollow = true;
    state.playbackIndex = Number(el.playback_slider.value);
    stopLiveScreenQueue();
    stopPlayback();
    renderAll();
  });
  el.live_button.addEventListener("click", () => {
    state.scoreOverlayIds.clear();
    hidePlaybackHover();
    state.playbackFreezeAt = 0;
    state.followLive = true;
    state.chatFollow = true;
    state.playbackIndex = state.timeline.length - 1;
    stopLiveScreenQueue();
    stopPlayback();
    renderAll();
    // Entering the live view: focus the terminal so its cursor blinks again.
    focusTerminalCursor();
  });
  el.play_button.addEventListener("click", () => {
    if (state.playing) { stopPlayback(); return; }
    state.scoreOverlayIds.clear();
    hidePlaybackHover();
    markPlaybackFrozen();
    state.chatFollow = true;
    stopLiveScreenQueue();
    state.playing = true;
    state.followLive = false;
    if (state.playbackIndex >= state.timeline.length - 1) state.playbackIndex = -1;
    el.play_button.textContent = "❚❚";
    const advancePlayback = () => {
      if (!state.playing) return;
      let next = state.playbackIndex + 1;
      // Coalesce human keystroke bookkeeping (received/started/completed per
      // stroke): land on the first event that actually changes what playback
      // shows and pay a single delay there, instead of one timer tick per
      // recorded keystroke event.
      while (next < state.timeline.length - 1 && isHumanKeystrokeEvent(eventAt(next))) next += 1;
      state.playbackIndex = Math.min(next, state.timeline.length - 1);
      renderAll();
      if (state.playbackIndex >= state.timeline.length - 1) {
        state.playbackIndex = state.timeline.length - 1;
        stopPlayback();
        return;
      }
      state.timer = setTimeout(advancePlayback, eventDelay(eventAt(state.playbackIndex)));
    };
    advancePlayback();
  });
  el.inspect_button.addEventListener("click", () => {
    const event = eventAt(state.followLive ? state.timeline.length - 1 : state.playbackIndex);
    el.event_title.textContent = event ? `${event.kind} · ${event.event_id}` : "没有事件";
    el.event_content.textContent = event ? JSON.stringify(event, null, 2) : "";
    el.event_dialog.showModal();
  });
  el.close_event.addEventListener("click", () => el.event_dialog.close());
}

function stopPlayback() {
  state.playing = false;
  clearTimeout(state.timer);
  state.timer = null;
  el.play_button.textContent = "▶";
}

async function boot() {
  bind();
  try {
    const session = await api("/api/session");
    state.csrfToken = session.csrf_token;
    state.csrfHeader = session.csrf_header || state.csrfHeader;
    await Promise.all([loadHealth(), loadMeetings(), loadSettings()]);
    if (state.meetings.length) await selectMeeting(state.meetings[0].meeting_id);
    else renderAll();
  } catch (error) { toast(error.message); }
  setInterval(() => { if (state.snapshot) renderHeader(); }, 1000);
}

boot();
