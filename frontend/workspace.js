"use strict";

const LIVE_SCREEN_FRAME_MS = 700;
const MAX_LIVE_SCREEN_QUEUE = 120;

const el = Object.fromEntries([
  "health-dot", "health-message", "new-button", "welcome-new-button", "meeting-count", "meeting-list", "list-empty",
  "welcome", "session-view", "meeting-title", "status-badge", "owner-badge", "session-meta",
  "phase-label", "backend-label", "provider-label", "model-label", "typing-profile-label", "action-label", "runtime-label", "event-count", "failure-message", "refresh-button",
  "resume-button", "complete-button", "cleanup-button", "stop-button", "chat-list", "message-form",
  "message-input", "send-message-button", "message-hint", "delivery-mode", "reference-card", "composer-shell", "evaluation-panel", "evaluation-form", "evaluation-excerpt", "evaluation-comment", "evaluation-error", "evaluation-submit-button", "evaluation-back-button", "evaluation-cancel-button", "terminal-command", "terminal-path",
  "terminal-screen", "typing-state", "terminal-readonly", "control-label", "acquire-button", "release-button", "terminal-controls",
  "command-form", "command-select", "command-path", "vim-form", "vim-text", "process-input-label", "play-button",
  "playback-track", "playback-slider", "playback-position", "playback-hover", "live-button", "inspect-button", "free-scroll-button", "follow-scroll-button", "settings-button", "settings-dialog", "settings-form", "judge-name-input", "save-settings", "close-settings", "cancel-settings", "settings-error", "terminal-score-status", "selection-menu", "new-dialog", "new-form",
  "close-dialog", "cancel-new", "initial-message", "backend-select", "provider-select", "model-input", "start-button", "new-error",
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
  followLive: true,
  viewportMode: "follow",
  chatFollow: true,
  renderingChat: false,
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
  phase: { created: "等待启动", starting: "正在打开终端", agent_thinking: "Agent 思考中", agent_acting: "Agent 操作中", waiting_for_user: "等待你的消息", completion_proposed: "Agent 提议完成", human_control: "由你控制", api_error: "模型连接中断", cycle_timeout: "本轮已超时", completed: "已结束", failed: "执行失败", interrupted: "已中止" },
  owner: { agent: "Agent 控制", human: "你在控制", none: "无人控制" },
  message: { request: "请求", ack: "回应", progress: "进度", question: "问题", final: "结果" },
  typing: { demo: "演示", balanced: "平衡", fast: "快速" },
  score: { competent: "胜任", incompetent: "不胜任", unscored: "无评分" },
  backend: { responses: "Responses API", "codex-app-server": "Codex App Server", "claude-code": "Claude Code" },
  provider: { openai: "OpenAI", deepseek: "DeepSeek" },
};

function eventDelay(event, queueLength = 0) {
  if (event?.kind !== "screen") return 250;
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
  if (!state.followLive) return;
  const screens = events.filter((event) => event.kind === "screen");
  if (!screens.length) return;
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
    button.append(top, node("span", "meeting-item-meta", `${labels.phase[meeting.phase] || meeting.phase} · ${provider} · ${fmtTime(meeting.updated_at)}`));
    button.addEventListener("click", () => selectMeeting(meeting.meeting_id));
    el.meeting_list.append(button);
  }
}

function renderHeader() {
  if (!state.snapshot) return;
  const meeting = state.snapshot.meeting;
  el.meeting_title.textContent = meeting.meeting_id;
  el.status_badge.textContent = labels.status[meeting.status] || meeting.status;
  el.status_badge.dataset.status = meeting.status;
  el.owner_badge.textContent = labels.owner[meeting.control_owner] || meeting.control_owner;
  el.session_meta.textContent = `${meeting.session_name} · ${meeting.session_state} · ${labels.provider[meeting.agent_provider] || meeting.agent_provider || "OpenAI"}`;
  const failure = typeof meeting.failure === "string" ? meeting.failure.trim() : "";
  el.failure_message.textContent = failure;
  el.failure_message.hidden = !failure || !["api_error", "cycle_timeout", "failed"].includes(meeting.phase);
  el.phase_label.textContent = labels.phase[meeting.phase] || meeting.phase;
  el.backend_label.textContent = labels.backend[meeting.agent_backend] || meeting.agent_backend || "Responses API";
  el.provider_label.textContent = labels.provider[meeting.agent_provider] || meeting.agent_provider || "OpenAI";
  el.model_label.textContent = meeting.model;
  el.typing_profile_label.textContent = labels.typing[meeting.typing_profile] || "平衡";
  el.action_label.textContent = `${meeting.cycle_action_count || 0} / 60`;
  el.runtime_label.textContent = duration(meeting.created_at);
  el.event_count.textContent = String(meeting.event_count || state.timeline.length);

  const terminal = state.snapshot.latest_screen?.data || {};
  const displayedTerminal = state.followLive && state.liveScreenEvent?.data
    ? state.liveScreenEvent.data
    : terminal;
  el.terminal_command.textContent = displayedTerminal.current_command || "shell";
  el.terminal_path.textContent = displayedTerminal.current_path
    ? displayedTerminal.current_path.split("/").slice(-2).join("/")
    : "workspace";
  el.control_label.textContent = meeting.control_requested === "human" ? "等待 Agent 交出控制权" : (labels.owner[meeting.control_owner] || "无人控制");

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
  el.resume_button.textContent = meeting.phase === "completion_proposed" ? "继续工作" : "继续 Agent";
  el.acquire_button.hidden = true;
  el.release_button.hidden = meeting.control_owner !== "human";
  el.terminal_controls.hidden = true;
  const vimActive = ["vim", "vi"].includes(terminal.current_command);
  const pythonActive = String(terminal.current_command || "").toLowerCase().startsWith("python");
  const shellActive = ["bash", "zsh", "sh"].includes(terminal.current_command);
  el.command_form.hidden = !shellActive;
  el.vim_form.hidden = !(vimActive || pythonActive);
  el.process_input_label.textContent = vimActive ? "Vim 输入" : "Python 输入";
  document.querySelector(".key-row").hidden = !(vimActive || pythonActive);
}

function visibleChats() {
  return eventsAtPlayback().filter((event) => {
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

function renderChat() {
  const previousScrollTop = el.chat_list.scrollTop;
  const replayReference = scoreReferenceAtPlayback();
  let replayTarget = null;
  state.renderingChat = true;
  try {
    el.chat_list.replaceChildren();
    const chats = visibleChats();
    for (const event of chats) {
      const article = node("article", `message ${event.actor}`);
      article.dataset.eventId = event.event_id || "";
      const head = node("header", "message-head");
      head.append(
        node("strong", "", event.actor === "human" ? "你" : "Agent"),
        node("span", `message-type ${event.message_type || ""}`, labels.message[event.message_type] || "消息"),
        node("time", "", fmtTime(event.captured_at)),
      );
      const annotations = scoreAnnotationsForSource(event);
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
    }
    if (!chats.length) el.chat_list.append(node("div", "empty-chat", "这个回放位置还没有聊天消息"));
    if (replayTarget) {
      const listRect = el.chat_list.getBoundingClientRect();
      const targetRect = replayTarget.getBoundingClientRect();
      const targetTop = el.chat_list.scrollTop + targetRect.top - listRect.top;
      el.chat_list.scrollTop = Math.max(0, targetTop - (el.chat_list.clientHeight - targetRect.height) / 2);
    } else if (state.chatFollow) {
      el.chat_list.scrollTop = el.chat_list.scrollHeight;
    } else {
      const maxScroll = Math.max(0, el.chat_list.scrollHeight - el.chat_list.clientHeight);
      el.chat_list.scrollTop = Math.min(previousScrollTop, maxScroll);
    }
  } finally {
    state.renderingChat = false;
  }
}

function renderTerminal() {
  const previousScrollTop = el.terminal_screen.scrollTop;
  const screens = eventsAtPlayback().filter((event) => event.kind === "screen");
  const replayReference = scoreReferenceAtPlayback();
  const replaySource = replayReference?.kind === "screen"
    ? state.timeline.find(
      (event) => event.kind === "screen" && event.event_id === replayReference.event_id,
    )
    : null;
  const latest = replaySource || (state.followLive && state.liveScreenEvent ? state.liveScreenEvent : screens.at(-1));
  const fallback = state.followLive ? state.snapshot?.latest_screen : null;
  const source = latest || fallback;
  const terminal = source?.data || {};
  el.terminal_command.textContent = terminal.current_command || "shell";
  el.terminal_path.textContent = terminal.current_path
    ? terminal.current_path.split("/").slice(-2).join("/")
    : "workspace";
  el.terminal_screen.replaceChildren();
  const hasScreenContent = Boolean(source && typeof source.content === "string" && source.content.length);
  el.terminal_screen.dataset.eventId = hasScreenContent ? source.event_id || "" : "";
  if (hasScreenContent) {
    appendAnnotatedText(el.terminal_screen, source.content, scoreAnnotationsForSource(source));
  } else {
    el.terminal_screen.append(document.createTextNode("等待终端画面…"));
  }
  el.terminal_screen.classList.toggle(
    "reference-target",
    Boolean(
      (state.highlightReference?.kind === "screen" && source?.event_id === state.highlightReference.event_id)
      || (replayReference?.kind === "screen" && source?.event_id === replayReference.event_id)
    ),
  );
  el.terminal_score_status.replaceChildren();
  const terminalSummary = scoreSummary(scoreAnnotationsForSource(source));
  if (terminalSummary) el.terminal_score_status.append(...terminalSummary.childNodes);
  const changeKind = terminal.change_kind;
  el.typing_state.textContent = changeKind === "paste" ? "Agent 正在粘贴" : (changeKind === "typing" ? "Agent 输入中" : "");
  el.typing_state.hidden = !["typing", "paste"].includes(changeKind);
  if (state.viewportMode === "follow") {
    revealTerminalCursor(terminal);
  } else {
    const maxScroll = Math.max(0, el.terminal_screen.scrollHeight - el.terminal_screen.clientHeight);
    el.terminal_screen.scrollTop = Math.min(previousScrollTop, maxScroll);
  }
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
  el.playback_slider.title = "移动鼠标查看评分状态";
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
  if (!hasEvents) hidePlaybackHover();
  if (state.playbackHoverIndex !== null && state.playbackHoverIndex <= max) {
    const status = scoreStatusForEvent(eventAt(state.playbackHoverIndex));
    el.playback_hover.textContent = labels.score[status] || labels.score.unscored;
    el.playback_hover.dataset.status = status;
  }
}

function renderAll() {
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
  return element?.closest?.(".message-content, #terminal-screen") || null;
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
    const before = range.cloneRange();
    before.selectNodeContents(root);
    before.setEnd(range.startContainer, range.startOffset);
    const content = root.textContent || "";
    const start = codePointLength(before.toString());
    const end = start + codePointLength(range.toString());
    const length = codePointLength(content);
    if (start < 0 || end > length || end <= start) return null;
    const excerpt = codePointSlice(content, start, end);
    if (!excerpt.trim()) return null;
    return { start, end, excerpt };
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
    if (!startRoot || startRoot !== endRoot) return null;
    const source = sourceEventForRoot(startRoot);
    if (!source) return null;
    const offsets = selectionOffsets(startRoot, selection);
    if (!offsets) return null;
    const rect = range.getBoundingClientRect();
    return { ...offsets, kind: source.kind, event_id: source.event_id, root: startRoot, rect };
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
  const source = state.timeline.find((event) => event.event_id === candidate.event_id)
    || state.snapshot?.chat?.find((event) => event.event_id === candidate.event_id);
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

function scheduleSelectionMenu() {
  clearTimeout(state.selectionTimer);
  // While the primary button is held the user is still dragging a selection;
  // the menu must only appear after release (pointerup/mouseup/touchend).
  if (state.pointerSelecting) {
    hideSelectionMenu();
    return;
  }
  state.selectionTimer = setTimeout(() => {
    state.selectionTimer = null;
    if (state.selectionBusy || state.pointerSelecting) return;
    const candidate = selectionCandidate();
    if (candidate) showSelectionMenu(candidate);
    else hideSelectionMenu();
  }, 45);
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
  state.healthy = Boolean(health.core_ok && backendEntries.some((item) => item.ok || item.needs_api_key));
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

function syncBackendSelectors(preferCurrent = true) {
  if (!el.backend_select || !el.provider_select) return;
  const entries = Object.values(state.health?.backends || {});
  const usable = (item) => item.ok || item.needs_api_key;
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
  updateCredentialFields();
}

function updateCredentialFields() {
  if (!el.api_key_input) return;
  const entry = backendEntry(el.backend_select.value, el.provider_select.value);
  const provider = el.provider_select.value;
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
  renderMeetingList();
}

async function selectMeeting(meetingId) {
  stopLiveScreenQueue();
  hideSelectionMenu();
  hidePlaybackHover();
  clearBrowserSelection();
  closeEvaluationPanel();
  setReferences([]);
  state.scoreOverlayIds.clear();
  state.selectedId = meetingId;
  const [snapshot, eventPayload] = await Promise.all([
    api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`),
    api(`/api/meetings/${encodeURIComponent(meetingId)}/events?after=0`),
  ]);
  if (meetingId !== state.selectedId) return;
  state.snapshot = snapshot;
  state.timeline = eventPayload.events || [];
  state.cursor = eventPayload.cursor || 0;
  state.followLive = true;
  state.chatFollow = true;
  state.playbackIndex = state.timeline.length - 1;
  renderAll();
  schedulePoll();
}

async function refreshSelected() {
  const meetingId = state.selectedId;
  if (!meetingId) return;
  const cursor = state.cursor;
  const [snapshot, eventPayload] = await Promise.all([
    api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`),
    api(`/api/meetings/${encodeURIComponent(meetingId)}/events?after=${cursor}`),
  ]);
  if (meetingId !== state.selectedId) return;
  state.snapshot = snapshot;
  const incoming = eventPayload.events || [];
  const additions = mergeTimelineEvents(incoming);
  if (additions.length) enqueueLiveScreens(additions);
  state.cursor = Math.max(Number(eventPayload.cursor) || 0, state.cursor);
  if (state.followLive) state.playbackIndex = state.timeline.length - 1;
  renderAll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (!state.selectedId) return;
  const active = state.snapshot && !["completed", "failed", "interrupted"].includes(state.snapshot.meeting.status)
    && !["api_error", "cycle_timeout", "waiting_for_user", "completion_proposed", "human_control"].includes(state.snapshot.meeting.phase);
  state.pollTimer = setTimeout(async () => {
    try { await refreshSelected(); if (active) await loadMeetings(); } catch (error) { toast(error.message); }
    schedulePoll();
  }, active ? 500 : 2000);
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

function textTokens(value) {
  const lines = value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
  const tokens = [];
  lines.forEach((line, index) => {
    if (line) tokens.push({ kind: "literal", value: line });
    if (index < lines.length - 1) tokens.push({ kind: "key", value: "Enter" });
  });
  return tokens;
}

function bind() {
  el.new_button.addEventListener("click", openNewDialog);
  el.welcome_new_button.addEventListener("click", openNewDialog);
  el.settings_button.addEventListener("click", openSettingsDialog);
  el.close_dialog.addEventListener("click", () => el.new_dialog.close());
  el.cancel_new.addEventListener("click", () => el.new_dialog.close());
  el.backend_select.addEventListener("change", () => syncBackendSelectors(true));
  el.provider_select.addEventListener("change", () => syncBackendSelectors(true));
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
      const response = await api("/api/meetings", {
        method: "POST",
        body: JSON.stringify({
          message: el.initial_message.value,
          agent_backend: el.backend_select.value,
          agent_provider: el.provider_select.value,
          model: el.model_input.value.trim() || null,
          typing_profile: typingProfile,
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
  el.refresh_button.addEventListener("click", () => refreshSelected().catch((error) => toast(error.message)));
  el.acquire_button.addEventListener("click", () => postMeetingAction("control/acquire").catch((error) => toast(error.message)));
  el.release_button.addEventListener("click", () => postMeetingAction("control/release").catch((error) => toast(error.message)));
  el.resume_button.addEventListener("click", () => postMeetingAction("agent/resume").catch((error) => toast(error.message)));
  el.complete_button.addEventListener("click", () => { if (confirm("结束会话并保留 tmux 工作区？")) postMeetingAction("complete").catch((error) => toast(error.message)); });
  el.stop_button.addEventListener("click", () => { if (confirm("中止会话并清理 tmux 工作区？")) postMeetingAction("stop").catch((error) => toast(error.message)); });
  el.cleanup_button.addEventListener("click", () => postMeetingAction("cleanup").catch((error) => toast(error.message)));
  el.command_select.addEventListener("change", () => { el.command_path.disabled = el.command_select.value === "pwd"; });
  el.command_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await postMeetingAction("terminal/input", { type: "command", command: el.command_select.value, path: el.command_select.value === "pwd" ? "" : el.command_path.value }); }
    catch (error) { toast(error.message); }
  });
  el.vim_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const keys = textTokens(el.vim_text.value);
    if (!keys.length) return;
    const current = state.snapshot?.latest_screen?.data?.current_command || "";
    const inputType = String(current).toLowerCase().startsWith("python") ? "terminal_keys" : "vim_keys";
    try { await postMeetingAction("terminal/input", { type: inputType, keys }); el.vim_text.value = ""; }
    catch (error) { toast(error.message); }
  });
  document.querySelectorAll("[data-key]").forEach((button) => button.addEventListener("click", () => {
    const current = state.snapshot?.latest_screen?.data?.current_command || "";
    const inputType = String(current).toLowerCase().startsWith("python") ? "terminal_keys" : "vim_keys";
    postMeetingAction("terminal/input", { type: inputType, keys: [{ kind: "key", value: button.dataset.key }] }).catch((error) => toast(error.message));
  }));
  document.querySelectorAll("[data-chat-filter]").forEach((button) => button.addEventListener("click", () => {
    state.chatFilter = button.dataset.chatFilter;
    document.querySelectorAll("[data-chat-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderChat();
  }));
  for (const container of [el.chat_list, el.terminal_screen]) {
    container.addEventListener("pointerdown", (event) => {
      if (event.button === 0) state.pointerSelecting = true;
    });
    container.addEventListener("pointerup", scheduleSelectionMenu);
    container.addEventListener("mouseup", scheduleSelectionMenu);
    container.addEventListener("touchend", () => setTimeout(scheduleSelectionMenu, 70), { passive: true });
    container.addEventListener("focusin", handleSourceFocus);
    container.addEventListener("keydown", handleKeyboardSelection);
  }
  // Release anywhere (even outside the containers) ends the drag; re-evaluate
  // the selection once so the menu appears exactly at mouse release.
  document.addEventListener("pointerup", () => {
    if (!state.pointerSelecting) return;
    state.pointerSelecting = false;
    scheduleSelectionMenu();
  });
  document.addEventListener("pointercancel", () => { state.pointerSelecting = false; });
  document.addEventListener("selectionchange", scheduleSelectionMenu);
  document.addEventListener("pointerdown", (event) => {
    if (!el.selection_menu.contains(event.target)) {
      hideSelectionMenu();
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
  el.selection_menu.addEventListener("pointerdown", (event) => event.stopPropagation());
  el.selection_menu.querySelectorAll("[data-selection-action]").forEach((button) => {
    button.addEventListener("click", () => handleSelectionAction(button.dataset.selectionAction));
  });
  el.chat_list.addEventListener("scroll", () => {
    if (state.renderingChat) return;
    const remaining = el.chat_list.scrollHeight - el.chat_list.clientHeight - el.chat_list.scrollTop;
    state.chatFollow = remaining <= 12;
  }, { passive: true });
  el.free_scroll_button.addEventListener("click", () => setViewportMode("free"));
  el.follow_scroll_button.addEventListener("click", () => setViewportMode("follow"));
  el.playback_slider.addEventListener("mousemove", showPlaybackHover);
  el.playback_slider.addEventListener("mouseleave", hidePlaybackHover);
  el.playback_slider.addEventListener("pointercancel", hidePlaybackHover);
  el.playback_slider.addEventListener("input", () => {
    state.scoreOverlayIds.clear();
    state.followLive = false;
    state.playbackIndex = Number(el.playback_slider.value);
    stopLiveScreenQueue();
    stopPlayback();
    renderAll();
  });
  el.live_button.addEventListener("click", () => {
    state.scoreOverlayIds.clear();
    hidePlaybackHover();
    state.followLive = true;
    state.playbackIndex = state.timeline.length - 1;
    stopLiveScreenQueue();
    stopPlayback();
    renderAll();
  });
  el.play_button.addEventListener("click", () => {
    if (state.playing) { stopPlayback(); return; }
    state.scoreOverlayIds.clear();
    hidePlaybackHover();
    stopLiveScreenQueue();
    state.playing = true;
    state.followLive = false;
    if (state.playbackIndex >= state.timeline.length - 1) state.playbackIndex = -1;
    el.play_button.textContent = "❚❚";
    const advancePlayback = () => {
      if (!state.playing) return;
      state.playbackIndex += 1;
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
