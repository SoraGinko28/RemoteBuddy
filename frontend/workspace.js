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
  "health-message", "welcome-new-button", "meeting-list", "list-empty",
  "add-workspace-button",
  "welcome", "session-view", "agent-states", "status-badge", "owner-badge",
  "workspace-menu", "meeting-menu", "drop-line",
  "welcome-advanced-button",
  "phase-label", "backend-label", "provider-label", "model-label", "typing-profile-label", "action-label", "runtime-label", "event-count", "failure-message", "refresh-button",
  "member-panel", "member-panel-close",
  "member-name-label", "member-role-label", "member-state-label", "member-terminal-label",
  "member-chat-label", "member-last-label",
  "resume-button", "complete-button", "cleanup-button", "stop-button", "chat-list", "message-form",
  "message-input", "send-message-button", "reference-card", "composer-shell",
  "attach-image-button", "image-input", "attachment-strip",
  "image-lightbox", "image-lightbox-image", "image-lightbox-close", "terminal-command", "terminal-path",
  "terminal-screen", "typing-state", "terminal-readonly", "control-label", "acquire-button", "release-button",
  "terminal-copy-button", "terminal-copy-ansi-button",
  "play-button",
  "playback-track", "playback-slider", "playback-position", "playback-hover", "live-button", "inspect-button", "free-scroll-button", "follow-scroll-button", "selection-menu", "new-dialog", "new-form", "delete-dialog", "delete-form", "close-delete-dialog", "cancel-delete", "confirm-delete", "delete-message", "delete-error",
  "close-dialog", "cancel-new", "initial-message", "workspace-input", "choose-directory", "backend-select", "provider-select", "model-input", "cli-default-model-row", "cli-default-model", "api-credential-fields", "effort-select", "effort-live-select", "start-button", "new-error",
  "api-key-input", "base-url-input", "api-key-hint", "api-key-name",
  "skill-cell", "reload-skill-button", "restart-agent-button",
  "event-dialog", "event-title", "event-content", "close-event", "toast", "welcome-toast",
  "ticket-toggle", "ticket-panel", "ticket-title", "ticket-digest", "ticket-issue-button", "ticket-hint",
  "ticket-outcome", "ticket-details", "ticket-list", "ticket-record",
  "rename-dialog", "rename-form", "rename-input", "rename-error", "rename-close", "rename-cancel", "rename-submit",
].map((id) => [id.replaceAll("-", "_"), document.querySelector(`#${id}`)]));

const state = {
  csrfToken: "",
  csrfHeader: "X-RemoteBuddy-CSRF",
  healthy: false,
  meetings: [],
  workspaces: [],
  workspaceTitles: {},
  workspaceMenuPath: "",
  meetingMenuId: "",
  // 正开着重命名弹框的那场会（空串 = 没开）；renameBound 是弹框按钮只接一次线的开关。
  renameMeetingId: "",
  renameBound: false,
  // 正开着删除确认框的那场会（空串 = 没开）。
  deleteMeetingId: "",
  // 正在拖的那一样：kind（meeting/workspace）、node、scope、起点和落点（见 beginDrag）。
  drag: null,
  suppressClickUntil: 0,
  workspace: "",
  listed: [],
  workspaceFilled: false,
  selectedId: null,
  snapshot: null,
  tickets: null,
  pendingAttachments: [],
  ticketsEtag: "",
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
  toastMessage: "",
  references: [],
  // Legacy alias kept for extensions that still read the old singular field.
  reference: null,
  highlightReference: null,
  pendingSelection: null,
  selectionBusy: false,
  selectionTimer: null,
  selectionMenuDelay: 0,
  keyboardSelection: null,
  playbackHoverIndex: null,
  health: null,
  pointerSelecting: false,
  // Which member's details the header strip is showing; null = closed.
  detailsAgent: null,
  // 页面上"更新/重启"按下去之后，chip 上按住这个状态几秒：服务端那一段
  // （换文本、换进程）常常短过一次轮询，看不到就等于没有反馈。
  agentOpHold: null,
};

const labels = {
  status: { created: "已创建", starting: "启动中", running: "协作中", completed: "已完成", failed: "失败", interrupted: "已中止" },
  phase: { created: "等待启动", starting: "正在打开终端", agent_thinking: "{agent} 思考中", agent_acting: "{agent} 操作中", waiting_for_user: "等待你的消息", completion_proposed: "{agent} 提议完成", human_control: "由你控制", api_error: "模型连接中断", cycle_timeout: "本轮已超时", skill_reload: "正在更新 Skill", agent_restart: "正在重启 {agent} 进程", completed: "已结束", failed: "执行失败", interrupted: "已中止" },
  owner: { agent: "{agent} 控制", human: "你在控制", none: "无人控制" },
  typing: { demo: "演示", balanced: "平衡", fast: "快速" },
  backend: { responses: "Responses API", "codex-app-server": "Codex App Server", "trae-agent": "Trae Agent", "claude-code": "Claude Code" },
  provider: { openai: "OpenAI", deepseek: "DeepSeek" },
  agentState: { thinking: "思考", acting: "行动", idle: "待命", error: "异常", updating: "更新", restarting: "重启" },
  role: { actor: "行动者", assistant: "助理", critic: "批判者" },
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
// waiting) renders as the muted "待命" badge; error details stay in
// #failure-message.
const AGENT_STATE_PHASES = {
  starting: "thinking",
  agent_thinking: "thinking",
  agent_acting: "acting",
  skill_reload: "updating",
  agent_restart: "restarting",
  api_error: "error",
  cycle_timeout: "error",
  failed: "error",
};

function agentStateForPhase(phase) {
  return AGENT_STATE_PHASES[phase] || "idle";
}

// Per-member state for the header chips. The actor follows terminal
// ownership; every other member (a reviewer) is "active" while
// the newest thinking/acting band in the timeline belongs to it.
const IDLE_PHASE_BOUNDARY = new Set([
  "created", "waiting_for_user", "human_control", "completion_proposed", "cycle_timeout", "api_error",
]);

// If a terminal owner claims to be thinking/acting but authored no event for
// this long, the worker is likely orphaned (state parked after a crash);
// stop reporting a live turn. The meeting's own updated_at is the other half:
// a turn that just started has no band of its own yet, and the gap since the
// last one can be minutes (a ticket flow waits for the agent between steps) —
// the phase change itself keeps the meeting fresh.
const STALE_BAND_MS = 180_000;

function meetingAge(meeting) {
  const stamp = Date.parse(meeting?.updated_at || "");
  return Number.isFinite(stamp) ? Date.now() - stamp : Number.POSITIVE_INFINITY;
}

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
  const hold = state.agentOpHold;
  if (hold && hold.member === actor && Date.now() < hold.until) return hold.state;
  // 更新 / 重启 是页面上那两个按钮的状态：agent 进程此刻正在被换掉，
  // 所以它不属于"谁占着终端"那套判断，任何时候都直接显示。
  if (meeting.phase === "skill_reload" || meeting.phase === "agent_restart") {
    if (memberRoles(meeting)[actor] === "actor") return agentStateForPhase(meeting.phase);
    return "idle";
  }
  if (meeting.control_owner === actor) {
    const stateName = agentStateForPhase(meeting.phase);
    if ((stateName === "thinking" || stateName === "acting")
        && Date.now() - latestBandTime(actor) > STALE_BAND_MS
        && meetingAge(meeting) > STALE_BAND_MS) {
      return "error";  // parked/orphaned: nothing from it and nothing from the meeting
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

// 这场会里扮演 actor 的那个成员（多成员会议上就是名字叫 actor 的那个）。
function actorMemberName(meeting) {
  const roles = memberRoles(meeting);
  return Object.keys(roles).find((name) => roles[name] === "actor") || null;
}

// 按住一个状态给 chip 看：按下去的人要立刻看到它落地，服务端的阶段随后会接上。
function holdMemberState(stateName, ms = 3000) {
  const member = actorMemberName(state.snapshot?.meeting);
  if (!member) return;
  state.agentOpHold = { member, state: stateName, until: Date.now() + ms };
  renderMemberStates(state.snapshot.meeting);
}

function renderMemberStates(meeting) {
  if (!el.agent_states) return;
  const roles = memberRoles(meeting);
  el.agent_states.replaceChildren();
  for (const actor of Object.keys(roles)) {
    const stateName = memberState(actor, meeting);
    // The chip is the only way into a member's details: with several members
    // in one meeting, each one opens its own panel.
    const chip = node("button", `agent-state ${stateName}`);
    chip.type = "button";
    chip.dataset.agent = actor;
    chip.title = `查看 ${actor} 的详情`;
    chip.setAttribute("aria-expanded", String(state.detailsAgent === actor));
    const dot = document.createElement("span");
    dot.className = "agent-state-dot";
    dot.setAttribute("aria-hidden", "true");
    chip.append(dot, document.createTextNode(
      `${actor} · ${labels.agentState[stateName] || "待命"}`));
    chip.addEventListener("click", () => toggleMemberDetails(actor));
    el.agent_states.append(chip);
  }
  el.agent_states.hidden = false;
}

// The member list of a meeting: the recorded role map, with the legacy
// single-agent layout as the fallback.
function memberRoles(meeting) {
  const roles = meeting?.agent_name2role;
  return (roles && Object.keys(roles).length) ? roles : { agent: "actor" };
}

// 成员详情是从上边栏右侧滑下来的浮层：点别处、按 Esc、点那个叉都关得掉，
// 它不夺焦点、不压暗画面，终端也不会因此重排。
function closeMemberDetails() {
  el.member_panel?.classList.remove("open");
  if (!state.detailsAgent) return;
  state.detailsAgent = null;
  document.querySelectorAll(".agent-state").forEach((chip) => {
    chip.setAttribute("aria-expanded", "false");
  });
}

function toggleMemberDetails(actor) {
  state.detailsAgent = state.detailsAgent === actor ? null : actor;
  renderMemberStates(state.snapshot.meeting);
  renderMemberDetails(state.snapshot.meeting);
}

// Per-member facts for the header strip. Message counts come from the loaded
// timeline, so they cover what the human can actually see and scroll through.
function memberStats(actor) {
  const events = (state.timeline || []).filter(
    (event) => event.kind === "chat" && event.actor === actor);
  if (!events.length) return { count: 0, last: null };
  return { count: events.length, last: events[events.length - 1].captured_at };
}

function renderMemberDetails(meeting) {
  const actor = state.detailsAgent;
  if (!actor || !memberRoles(meeting)[actor]) {
    state.detailsAgent = null;
    el.member_panel?.classList.remove("open");
    return;
  }
  const role = memberRoles(meeting)[actor];
  const stats = memberStats(actor);
  el.member_name_label.textContent = actor;
  el.member_role_label.textContent = labels.role[role] || role;
  el.member_state_label.textContent = labels.agentState[memberState(actor, meeting)] || "待命";
  // Only the actor may drive the shared terminal; everyone else watches.
  el.member_terminal_label.textContent = role === "actor" ? "可写" : "只读";
  el.member_chat_label.textContent = `${stats.count} 条`;
  el.member_last_label.textContent = stats.last ? fmtTime(stats.last) : "—";
  if (stats.last) {
    el.member_last_label.title = formatFullTime(stats.last);
  } else {
    el.member_last_label.removeAttribute("title");
  }
  // Skill 那两个按钮只对 actor 有意义：这场会的那份 Skill 是它的说明书，
  // 助理和批判者跑在各自的过程里，不读它。
  if (el.skill_cell) el.skill_cell.hidden = role !== "actor";
  el.member_panel?.classList.add("open");
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

// The status travels with the error: callers that poll need to tell "this
// meeting is gone" (404) from a transient failure.
function httpError(response, payload) {
  const error = new Error(payload.error || `请求失败 (${response.status})`);
  error.status = response.status;
  return error;
}

async function api(path, options = {}, retryCsrf = true) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  // `raw` 用于图片那种二进制 body：不要塞 JSON 的 Content-Type。
  if (options.body !== undefined && !options.raw) headers.set("Content-Type", "application/json");
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
  if (!response.ok) throw httpError(response, payload);
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
  if (!response.ok) throw httpError(response, payload);
  return { notModified: false, etag: response.headers.get("ETag") || "", payload };
}

// `kind` 决定颜色：报错是红的，办成了是绿的，其余（"没变"、"已复制"这种）
// 是中性色——把"Skill 没变"画成红色会让人以为出事了。
function toast(message, kind = "error") {
  const sameVisibleToast = state.toastMessage === message
    && [el.toast, el.welcome_toast].some((node) => node && !node.hidden);
  if (sameVisibleToast) return;
  clearTimeout(state.toastTimer);
  state.toastMessage = message;
  // 两个位置同一句话：会话里那个在上边栏靠左，欢迎卡片上那个没有上边栏，贴左上角。
  const visible = [el.toast, el.welcome_toast].filter(Boolean);
  for (const node of visible) {
    node.textContent = message;
    node.dataset.kind = kind;
    node.hidden = false;
  }
  state.toastTimer = setTimeout(() => {
    for (const node of visible) node.hidden = true;
  }, 5000);
}
globalThis.workspaceToast = toast;

function formatFullTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const seconds = Math.floor(Math.max(0, Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}天前`;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
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

// Human input is journaled as bookkeeping only (received/started/completed):
// what was typed is never stored, so the record says an action happened, by
// whom and when. During playback those events carry no visuals of their own --
// the takeover terminal paints through the interleaved screen frames -- so
// auto-play coalesces them: a whole run is skipped in one step and rendering
// resumes at the next real event.
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
    return visible;
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

// A meeting lives in a workspace: its data sits in `<workspace>/.meeting/<id>/`,
// and several meetings may share one workspace. The group's name is the
// directory's basename — the project you typed when you opened the session.
// The workspaces come first: one can be added before it has any meeting, so an
// empty group is a group too.
function meetingGroups() {
  const groups = [];
  const byPath = new Map();
  const groupFor = (path) => {
    if (!byPath.has(path)) {
      const one = { path, title: state.workspaceTitles[path] || "", meetings: [] };
      byPath.set(path, one);
      groups.push(one);
    }
    return byPath.get(path);
  };
  for (const workspace of state.workspaces) groupFor(workspace);
  for (const meeting of state.meetings) {
    groupFor(meeting.workspace_root || state.workspace || "").meetings.push(meeting);
  }
  // The default workspace is the directory the service itself runs in: it stays
  // out of the list until it holds a session or somebody added it by hand.
  return groups.filter((group) => group.meetings.length > 0
    || group.path !== state.workspace
    || state.listed.includes(group.path));
}

function basename(path) {
  const parts = String(path).split("/").filter(Boolean);
  return parts.length ? parts.at(-1) : path;
}

// "3 分钟前" reads faster than a wall clock when the question is "which one was
// I just in"; the exact time stays in the title.
function fmtAgo(value) {
  const then = new Date(value).getTime();
  if (!Number.isFinite(then)) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return "刚刚";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

// 兜底标题：会话还没说过话，列表里也不该出现会议号。
const NEW_MEETING_TITLE = "新会话";
function meetingLabel(meeting) {
  return meeting.title || NEW_MEETING_TITLE;
}

// 一条会话就一行：标题 + 相对时间。会议号、provider、阶段都不占地方（会议号在悬停
// 的提示里，header 也一样）。鼠标悬在这一行上时时间让位给「···」（会话操作、删除在
// 那个浮层里）；调顺序是右键按住这一行上下拖，见 beginDrag。
function meetingItem(meeting) {
  const row = node("div", "meeting-row");
  row.dataset.meeting = meeting.meeting_id;
  row.dataset.workspace = meeting.workspace_root || state.workspace || "";
  const button = node("button", `meeting-item${meeting.meeting_id === state.selectedId ? " selected" : ""}`);
  button.type = "button";
  button.title = meeting.meeting_id;
  button.append(
    node("span", "meeting-item-title", meetingLabel(meeting)),
    node("span", "meeting-item-time", fmtAgo(meeting.updated_at)),
  );
  const more = node("button", "meeting-item-more", "···");
  more.type = "button";
  more.title = "会话操作";
  more.setAttribute("aria-label", "会话操作");
  more.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleMeetingMenu(meeting, more);
  });
  // A drag across the title is the human selecting it to copy, not a command
  // to open the meeting: a click that ends with a selection inside this item
  // must not navigate, which would also rebuild the list and drop it.
  button.addEventListener("click", () => {
    if (Date.now() < (state.suppressClickUntil || 0)) return;   // 刚拖完，别打开
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && button.contains(selection.anchorNode)) return;
    selectMeeting(meeting.meeting_id);
  });
  // 右键按住上下拖 = 调整顺序（左键留给选字复制和打开会话）。
  row.addEventListener("pointerdown", (event) => beginDrag(event, "meeting", row));
  row.append(button, more);
  return row;
}

// 工作区那一行前面那个文件夹图标（内联 SVG，跟着文字颜色走）。
function folderIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linejoin", "round");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z");
  svg.append(path);
  return svg;
}

function renderMeetingList() {
  // Rebuilding the items drops any selection inside them, so stand down while
  // the human is selecting something in the list (the next render, after the
  // selection is cleared, picks the change up). A drag in progress is the same
  // case: a redraw would take the row out from under the pointer.
  const live = window.getSelection();
  if (live && !live.isCollapsed && el.meeting_list.contains(live.anchorNode)) return;
  if (state.drag) return;
  el.meeting_list.replaceChildren();
  const groups = meetingGroups();
  // "No v2 session yet" is for having no workspace at all: a workspace that has
  // no meetings says so under itself.
  el.list_empty.hidden = groups.length > 0;
  for (const group of groups) {
    const section = node("section", "meeting-group");
    section.dataset.workspace = group.path;
    const head = node("div", "meeting-group-head");
    head.title = group.path;
    head.addEventListener("pointerdown", (event) => beginDrag(event, "workspace", section));
    head.append(
      folderIcon(),
      node("span", "meeting-group-name",
        group.title || (group.path ? basename(group.path) : "未知目录")),
      groupActions(group),
    );
    section.append(head);
    for (const meeting of group.meetings) section.append(meetingItem(meeting));
    if (!group.meetings.length) section.append(node("p", "meeting-group-empty", "还没有会话"));
    el.meeting_list.append(section);
  }
}

// The dialog's workspace field is a directory you fill in yourself: leave it
// empty for the default one, or point it at the project this session should
// work in. It remembers the last one so the usual case is a single click.
function renderWorkspaceHint() {
  if (!el.workspace_input) return;
  el.workspace_input.placeholder = state.workspace
    ? `留空就用 ${state.workspace}`
    : "留空就用默认目录";
  if (!state.workspaceFilled) {
    const recent = state.meetings[0] && state.meetings[0].workspace_root;
    if (recent && recent !== state.workspace) el.workspace_input.value = recent;
    state.workspaceFilled = true;
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

function selectionInside(node) {
  const selection = window.getSelection && window.getSelection();
  if (!selection || !selection.rangeCount || selection.isCollapsed || !node) return false;
  const anchor = selection.anchorNode;
  const element = anchor && anchor.nodeType === 1 ? anchor : anchor && anchor.parentNode;
  return Boolean(element && node.contains(element));
}

function renderHeader() {
  if (!state.snapshot) return;
  if (state.pointerSelecting) return;
  // 只在上边栏自己被选中时让路。整页只要有一个选区就把 header 冻住是错的：人在对话里选一段
  // 文字看，会那边已经又跑起来了，上边栏还挂着上一条错误提示（阶段、状态、提示全是旧的）。
  if (selectionInside(document.querySelector(".session-header"))) return;
  const meeting = state.snapshot.meeting;
  if (el.agent_states) {
    renderMemberStates(meeting);
  }
  renderMemberDetails(meeting);
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
  const addAction = el.selection_menu?.querySelector('[data-selection-action="add-to-conversation"]');
  if (addAction) addAction.disabled = terminalState;
  el.message_input.disabled = terminalState;
  el.send_message_button.disabled = terminalState;
  el.complete_button.disabled = terminalState || ["created", "starting"].includes(meeting.status) || meeting.phase === "starting";
  el.stop_button.disabled = terminalState;
  el.complete_button.hidden = terminalState;
  el.stop_button.hidden = terminalState;
  el.cleanup_button.hidden = !terminalState || meeting.session_state === "stopped";
  el.resume_button.hidden = !["api_error", "cycle_timeout", "waiting_for_user", "completion_proposed"].includes(meeting.phase) || terminalState;
  el.resume_button.textContent = meeting.phase === "completion_proposed" ? "继续工作" : `继续 ${agentName(meeting)}`;
  const takeoverLive = meeting.control_owner === "human" && meeting.phase === "human_control";
  const waitingHandoff = meeting.control_requested === "human" && meeting.control_owner !== "human";
  // 终端会话没了（tmux 被杀、机器重启）也能接管：服务会在同一个工作区里重开一个，
  // 所以这里只提示会发生什么，不把按钮关掉。
  const sessionGone = meeting.session_state === "stopped";
  const canAcquire = !terminalState && meeting.status === "running"
    && ["agent_thinking", "agent_acting", "waiting_for_user", "completion_proposed", "api_error", "cycle_timeout"].includes(meeting.phase);
  el.acquire_button.hidden = terminalState || takeoverLive;
  el.acquire_button.disabled = !canAcquire;
  el.acquire_button.textContent = waitingHandoff ? "等待交权…" : "接管";
  el.acquire_button.title = sessionGone
    ? "终端会话已结束：接管会在这个工作区里重开一个终端"
    : (canAcquire ? `打断 ${agentName(meeting)} 并接管共享终端` : "");
  el.release_button.hidden = !takeoverLive;
  const terminalReadonly = el.terminal_readonly;
  if (terminalReadonly) {
    terminalReadonly.textContent = takeoverLive ? "已接管 · 实时终端" : "只读预览";
    terminalReadonly.classList.toggle("takeover-live", takeoverLive);
  }
  el.message_input.placeholder = takeoverLive
    ? `接管中：输入消息会结束接管并交给 ${agentName(meeting)}…`
    : `继续说明、纠正或打断 ${agentName(meeting)}…`;
  syncTerminalTakeover(takeoverLive);
}

// The chat transcript is the meeting's narrative, so it also carries the
// terminal-control records (ADT: data.status == "terminal_control"). They are
// not messages and never answer the chat filter: taking the keyboard is not
// chatter, and hiding it would misreport who was working.
function visibleChats() {
  // The chat transcript is never clipped by the terminal playback cursor:
  // the scrubber replays screens, while the conversation stays fully
  // readable both before and after the current point.
  return state.timeline.filter((event) => {
    if (event.kind === "control") return isTerminalControl(event);
    if (event.kind !== "chat") return false;
    if (state.chatFilter === "result") return ["request", "question", "final"].includes(event.message_type);
    if (state.chatFilter === "progress") return ["ack", "progress"].includes(event.message_type);
    return true;
  });
}

// The ADT TerminalControl record, as opposed to the acquired/released
// annotations the journal also writes under the same event kind.
function isTerminalControl(event) {
  return event?.kind === "control" && event.data?.status === "terminal_control";
}

// A control record carries the line the transcript shows (the engine writes it
// with the record, since 接管/交还 depends on the record before it), so the
// chat area, the selection toolbar and the server all quote the same text.
function terminalControlLine(event) {
  return String(event?.content || "");
}

function referenceKindLabel(kind) {
  if (kind === "screen") return "Screen 引用";
  if (kind === "terminal_control") return "终端接管";
  return "Chat 引用";
}

// The ADT name of the reference that points at this event.
function referenceKindForEvent(event) {
  return isTerminalControl(event) ? "terminal_control" : (event?.kind || "");
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
    (event) => event.event_id === eventId
      && (event.kind === "chat" || event.kind === "screen" || isTerminalControl(event)),
  );
  if (fromTimeline) return fromTimeline;
  const latest = state.snapshot?.latest_screen;
  return latest?.event_id === eventId ? latest : null;
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
    for (const event of chats) {
      if (isTerminalControl(event)) {
        const line = node("div", "chat-control-line");
        line.dataset.eventId = event.event_id || "";
        line.append(node("span", "", terminalControlLine(event)));
        el.chat_list.append(line);
        const lineSequence = Number(event.seq);
        if (!state.followLive && Number.isFinite(lineSequence) && lineSequence <= chatCursorSeq) {
          cursorArticle = line;
        }
        continue;
      }
      const article = node("article", `message ${event.actor}`);
      article.dataset.eventId = event.event_id || "";
      const head = node("header", "message-head");
      const timeEl = node("time", "", fmtTime(event.captured_at));
      timeEl.title = formatFullTime(event.captured_at);
      timeEl.addEventListener("click", () => jumpToChatTime(event));
      const meetingSnapshot = state.snapshot?.meeting;
      // Show the record's own speaker names: human = 你, every agent (and
      // future role-map member) labels by its actor name as recorded.
      const speaker = event.actor === "human"
        ? "你"
        : (event.actor || agentName(meetingSnapshot));
      head.append(
        node("strong", "", speaker),
        timeEl,
      );
      if (event.message_type === "final") {
        const eventSeq = Number(event.seq);
        const nextFinalSeq = state.timeline
          .filter((item) => item.kind === "chat"
            && item.message_type === "final"
            && Number(item.seq) > eventSeq)
          .map((item) => Number(item.seq))
          .sort((a, b) => a - b)[0] ?? Infinity;
        const completion = state.timeline.find((item) => item.kind === "state"
          && item.phase === "completion_proposed"
          && Number(item.seq) > eventSeq
          && Number(item.seq) < nextFinalSeq);
        const evidence = Boolean((event.references && event.references.length)
          || completion?.data?.evidence === true);
        head.append(node("span", evidence ? "completion-evidence" : "completion-no-evidence",
          evidence ? "完成提议 · 有证据" : "完成提议 · 无证据"));
      }
      const images = messageImages(event);
      const body = node("p", "message-content");
      body.dataset.eventId = event.event_id || "";
      body.tabIndex = 0;
      body.setAttribute("aria-label", "聊天消息文本");
      body.append(document.createTextNode(String(event.content ?? "")));
      article.append(head);
      const references = referencesForEvent(event);
      // 引用和图片都放在正文前面：先看见"这句话在说哪一段、盯着哪张图"，再读这句话。
      // 只有图片没有字的消息，也不留一个空段落。
      references.forEach((reference, referenceIndex) => {
        if (!reference?.excerpt) return;
        // The quote stands on its own: no "引用" label in front of it, only a
        // number when there is more than one and they need telling apart.
        const prefix = references.length > 1 ? `${referenceIndex + 1} · ` : "";
        const quote = node("blockquote", "message-reference", `${prefix}${reference.excerpt}`);
        quote.title = "跳转到引用来源";
        quote.addEventListener("click", () => {
          const index = state.timeline.findIndex((item) => item.event_id === reference.event_id);
          if (index < 0) return;
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
      if (images) article.append(images);
      if (event.content) article.append(body);
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
    if (!chats.length) el.chat_list.append(node("div", "empty-chat", "还没有消息 · 说点什么，Agent 就在这个目录里开始"));
    // All scrollTop writes go through here so the follow-scroll renderChat
    // applies can be told apart from a genuine user scroll: the chat scroll
    // handler must not pause following because of its own programmatic scroll.
    const scrollChatTo = (value) => {
      const maxScroll = Math.max(0, el.chat_list.scrollHeight - el.chat_list.clientHeight);
      const target = Math.max(0, Math.min(value, maxScroll));
      state.chatScrollApplied = target;
      el.chat_list.scrollTop = target;
    };
    if (state.chatFollow) {
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
    toast("没有可复制的内容", "info");
    return false;
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(() => {
      if (!legacyCopy(text)) copyFallbackDialog(text);
    });
    return true;
  }
  if (legacyCopy(text)) return true;
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
        toast(`已选中 ${selected.length} 字符 · ⌘/Ctrl+C 复制`, "info");
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
        background: "#101413",
        foreground: "#e7eee9",
        cursor: "#c5cdc8",
        selectionBackground: "#6091ff",
        selectionForeground: "#101413",
        black: "#1c1f1e", red: "#e06c75", green: "#98c379", yellow: "#d19a66",
        blue: "#61afef", magenta: "#c678dd", cyan: "#56b6c2", white: "#abb2bf",
        brightBlack: "#5c6370", brightRed: "#e06c75", brightGreen: "#98c379",
        brightYellow: "#d19a66", brightBlue: "#61afef", brightMagenta: "#c678dd",
        brightCyan: "#56b6c2", brightWhite: "#ffffff",
      },
    });
    el.terminal_screen.replaceChildren();
    el.terminal_screen.dataset.geometryReady = "false";
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
  scheduleHumanTerminalResize();
  renderTerminal();
}

let humanTerminalResizeTimer = null;
let humanTerminalResizeKey = "";
let humanTerminalResizeInFlight = false;

function scheduleHumanTerminalResize() {
  if (!state.selectedId || !terminalEngine || (typeof takeoverStream !== "undefined" && takeoverStream.active)) return;
  if (humanTerminalResizeTimer !== null) clearTimeout(humanTerminalResizeTimer);
  humanTerminalResizeTimer = setTimeout(() => {
    humanTerminalResizeTimer = null;
    resizeHumanTerminal().catch(() => {});
  }, 120);
}

async function resizeHumanTerminal(force = false) {
  if (!state.selectedId || !terminalEngine || (typeof takeoverStream !== "undefined" && takeoverStream.active)) return;
  const grid = takeoverGrid(terminalEngine.term);
  const panel = el.terminal_screen;
  const key = `${state.selectedId}:${grid.cols}x${grid.rows}`;
  const pane = state.snapshot?.latest_screen?.data || {};
  const paneMatchesGrid = Number(pane.pane_width) === grid.cols && Number(pane.pane_height) === grid.rows;
  if (humanTerminalResizeInFlight || (!force && key === humanTerminalResizeKey) || (force && paneMatchesGrid)) return;
  humanTerminalResizeInFlight = true;
  try {
    const result = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/terminal/resize`, {
      method: "POST",
      body: JSON.stringify({
        ...grid,
        diagnostics: {
          panel_width: panel?.clientWidth || 0,
          panel_height: panel?.clientHeight || 0,
          cols: grid.cols,
          rows: grid.rows,
          window_width: window.innerWidth,
          window_height: window.innerHeight,
          device_pixel_ratio: window.devicePixelRatio,
        },
      }),
    });
    const engine = terminalEngine.term;
    if (engine.cols !== result.pane_width || engine.rows !== result.pane_height) {
      engine.resize(result.pane_width, result.pane_height);
    }
    termRowsState = null;
    state.terminalFrameKey = null;
    humanTerminalResizeKey = `${state.selectedId}:${result.pane_width}x${result.pane_height}`;
    renderTerminal();
  } finally {
    humanTerminalResizeInFlight = false;
  }
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
  takeoverStream.inputHex = "";
  takeoverStream.flushing = true;
  const body = { bytes: hex };
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
function renderedTerminalCellWidth(engine) {
  try {
    const width = Number(engine?._core?._renderService?.dimensions?.css?.cell?.width);
    return width > 0 ? width : TERMINAL_FONT_SIZE * 0.6;
  } catch (_error) {
    return TERMINAL_FONT_SIZE * 0.6;
  }
}

function takeoverGrid(engine) {
  const terminalInner = el.terminal_screen.querySelector(".xterm") || el.terminal_screen;
  const panelWidth = Math.max(0, (el.terminal_screen.clientWidth || 0) - 8);
  const panelHeight = el.terminal_screen.clientHeight || 0;
  const charWidth = renderedTerminalCellWidth(engine);
  const cellHeight = terminalCellHeight(engine);
  let cols = 80;
  if (panelWidth > 0 && charWidth > 0) {
    cols = Math.max(40, Math.min(300, Math.floor(panelWidth / charWidth)));
  }
  let rows = 24;
  if (panelHeight > 0 && cellHeight > 0) rows = Math.floor(panelHeight / TERMINAL_ROW_PX);
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
  globalThis.__takeoverReady = true;
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
  // The takeover stream must not expose xterm before its first real seed has
  // been laid out; otherwise the old cell width can be visible briefly.
  el.terminal_screen.dataset.geometryReady = "false";
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
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (takeoverStream.active && takeoverStream.source === source) {
          el.terminal_screen.dataset.geometryReady = "true";
        }
      });
    });
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
  globalThis.__takeoverReady = false;
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
  const previousRows = termRowsState;
  const geometryChanged = !previousRows
    || previousRows.width !== width
    || previousRows.height !== height
    || previousRows.plain.length !== rows.length;
  if (hasWaiting || geometryChanged) {
    term.reset();
    term.write(rows.map((row, index) =>
      `\x1b[${index + 1};1H` + row.ansi + "\x1b[K"
    ).join(""));
  } else {
    const changed = rows.reduce((indexes, row, index) => {
      if (previousRows.plain[index] !== row.plain) indexes.push(index);
      return indexes;
    }, []);
    term.write(changed.map((index) =>
      `\x1b[${index + 1};1H` + rows[index].ansi + "\x1b[K"
    ).join(""));
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
  // A clicked citation is also a reason to show a particular frame: the quote
  // points at the moment the rules judged, so the terminal has to go there.
  // Live mode always shows the newest captured frame: replaying the backlog
  // frame-by-frame made the terminal lag seconds behind the chat, which had
  // already rendered the agent's finished reply.
  const latest = screens.at(-1);
  const fallback = state.followLive ? state.snapshot?.latest_screen : null;
  const source = latest || fallback;
  const terminal = source?.data || {};
  el.terminal_command.textContent = terminal.current_command || "shell";
  el.terminal_path.textContent = terminal.current_path
    ? terminal.current_path.split("/").slice(-2).join("/")
    : "workspace";
  const hasScreenContent = Boolean(source && typeof source.content === "string" && source.content.length);
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
    const cellHeight = TERMINAL_ROW_PX;
    let fittingRows = fullRows;
    if (panelHeight > 0) {
      fittingRows = Math.floor(panelHeight / cellHeight);
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
      if (panelHeight > 0 && viewRows > 0) {
        const fittedLineHeight = TERMINAL_LINE_HEIGHT * panelHeight / (viewRows * TERMINAL_ROW_PX);
        if (Number.isFinite(fittedLineHeight) && fittedLineHeight >= 1) {
          engine.options.lineHeight = fittedLineHeight;
          engine.resize(width, viewRows);
        }
      }
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
        // A cited region is shown from its own first row: the quote is what the
        // reader came to see, and the cursor that produced the frame may sit
        // far below it (a `:wq` at the bottom of a file, for one).
        const cited = state.highlightReference?.kind === "screen"
          && state.highlightReference.event_id === source.event_id
          ? Number(state.highlightReference.region?.start)
          : NaN;
        if (Number.isFinite(cited) && hasScreenContent) {
          const citedRow = source.content.slice(0, Math.max(0, cited)).split("\n").length - 1;
          rowOffset = Math.max(0, Math.min(fullRows - viewRows, citedRow));
        } else {
          const anchorRow = Number.isFinite(cursorY) ? cursorY : fullRows - 1;
          rowOffset = Math.max(0, Math.min(fullRows - viewRows, anchorRow - viewRows + 1));
        }
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
      // Normal playback has no takeover seed event to flip the gate. Reveal
      // it only after the first frame has been laid out and painted.
      if (el.terminal_screen.dataset.geometryReady !== "true") {
        requestAnimationFrame(() => {
          if (!takeoverStream.active && terminalEngine?.term === engine) {
            el.terminal_screen.dataset.geometryReady = "true";
          }
        });
      }
    }
    if (state.viewportMode === "follow") engine.scrollToBottom();
  } else {
    // Legacy DOM text rendering (used when xterm.js did not load).
    const previousScrollTop = el.terminal_screen.scrollTop;
    el.terminal_screen.replaceChildren();
    if (hasScreenContent) {
      el.terminal_screen.append(document.createTextNode(source.content));
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
    ),
  );
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

function hidePlaybackHover() {
  state.playbackHoverIndex = null;
  el.playback_hover.hidden = true;
  el.playback_hover.removeAttribute("data-status");
  el.playback_slider.title = "移动鼠标查看位置";
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
  state.playbackHoverIndex = index;
  el.playback_hover.textContent = `${index + 1} / ${state.timeline.length}`;
  el.playback_hover.removeAttribute("data-status");
  el.playback_hover.hidden = false;
  const halfWidth = Math.min(el.playback_hover.offsetWidth / 2, trackRect.width / 2);
  const rawLeft = clientX - trackRect.left;
  const left = Math.max(halfWidth, Math.min(trackRect.width - halfWidth, rawLeft));
  el.playback_hover.style.left = `${left}px`;
  el.playback_slider.title = `${index + 1} / ${state.timeline.length}`;
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
  renderTickets();
  renderChat();
  renderTerminal();
  renderPlayback();
}

function renderTickets() {
  const board = state.tickets;
  // 只有跑过流程的会才有这一块：别的会议（产品原来那些）不显示。
  const show = Boolean(board && board.ran);
  el.ticket_toggle.hidden = !show;
  el.ticket_panel.hidden = !show;
  if (!show) {
    el.session_view.classList.remove("tickets-open");
    return;
  }
  const tickets = board.tickets || [];
  const due = tickets.find((one) => one.due);
  const running = board.running;
  const verdicts = tickets.flatMap((one) => one.answers || [])
    .filter((step) => step.op === "judge" && typeof step.value === "boolean");
  const last = verdicts.length ? verdicts[verdicts.length - 1].value : null;
  el.ticket_toggle.textContent = [
    `工单 ${tickets.filter((one) => one.issued).length}/${tickets.length}`,
    last === null ? "" : (last ? "合格" : "不合格"),
    running ? "跑着" : due ? "该发" : "",
  ].filter(Boolean).join(" · ");
  el.ticket_digest.textContent = String(board.digest || "").slice(0, 8);
  el.ticket_issue_button.hidden = !due || Boolean(running);
  if (due) {
    el.ticket_issue_button.textContent = `发 ${due.ticket}`;
    el.ticket_issue_button.dataset.ticket = due.ticket;
  }
  const done = tickets.filter((one) => one.issued).length;
  el.ticket_hint.textContent = [
    `${done}/${tickets.length} 张发过了`,
    board.case ? `案子 ${lastSegment(board.case)}` : "还没有案子",
    board.target ? `要交 ${board.target}` : "",
    running ? `${running} 正在跑` : "",
  ].filter(Boolean).join("｜");
  el.ticket_outcome.textContent = outcomeText(tickets);
  el.ticket_list.replaceChildren(...tickets.map(ticketRow));
  el.ticket_record.textContent = (board.record || []).join("\n");
}

function outcomeText(tickets) {
  const answers = tickets.flatMap((one) => one.answers || []);
  const verdicts = answers.filter((step) => step.op === "judge" && typeof step.value === "boolean");
  const objection = answers.find((step) => step.op === "objection" && step.value);
  const closing = answers.find((step) => step.op === "closing_note" && step.value);
  const parts = [];
  if (verdicts.length) {
    parts.push(verdicts.map((step, index) => `第 ${index + 1} 判 ${step.value ? "合格" : "不合格"}`).join("、"));
  }
  if (objection) parts.push(`判语：${objection.value}`);
  if (closing) parts.push(`收尾：${closing.value}`);
  return parts.join("｜");
}

function ticketRow(one) {
  const row = node("div", `ticket-row${one.due ? " due" : ""}`);
  row.append(node("span", "ticket-name", one.ticket));
  row.append(node("span", `ticket-chip ${one.due ? "state-due" : one.issued ? "state-issued" : ""}`,
    one.due ? "该发" : one.issued ? "发过了" : "还没轮到"));
  const answers = one.answers || [];
  const answered = answers.filter((step) => step.answered);
  const roles = [...new Set(answered.map((step) => step.role).filter(Boolean))];
  row.append(node("span", "ticket-meta",
    `${roles.join("、") || "—"} 答了 ${answered.length}/${answers.length} 步`));
  const verdict = answers.find((step) => step.op === "judge" && typeof step.value === "boolean");
  const note = answers.find((step) => step.op === "closing_note" && step.value);
  if (verdict) row.append(node("span", `ticket-chip ${verdict.value ? "state-issued" : "state-bad"}`,
    verdict.value ? "合格" : "不合格"));
  if (note) row.append(node("span", "ticket-meta", note.value));
  // 水表（每条数据路径、谁答的）藏在悬停里：页面上看的是流程和结果，不是路径。
  row.title = [
    one.why,
    ...answers.map((step) => `${step.answered ? "答" : "缺"} ${step.path} + ${step.op}`
      + (step.role ? `（${step.role}）` : "")),
  ].join("\n");
  return row;
}

function lastSegment(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || String(path || "");
}

function lastSegments(path, count) {
  const parts = String(path || "").split("/").filter(Boolean);
  return parts.slice(-count).join("/") || String(path || "");
}

async function postTicket(ticket) {
  if (!state.selectedId || !ticket) return;
  try {
    await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/tickets/${encodeURIComponent(ticket)}`,
      { method: "POST", body: JSON.stringify({}) });
    await refreshSelected();
  } catch (error) {
    toast(error.message);
  }
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
    // 引的是哪一类，不占一行字：它是给悬浮提示的（聊天、终端画面、接管记录）。
    item.title = referenceKindLabel(reference.kind);
    item.append(node("span", "reference-excerpt", reference.excerpt || ""));
    // 右上角一个圆圈叉：悬浮在引文上方，不占版面。
    const remove = node("button", "reference-remove");
    remove.type = "button";
    remove.title = `移除第 ${index + 1} 个引用`;
    remove.setAttribute("aria-label", remove.title);
    remove.append(closeCrossIcon());
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
    // Chat message text stays selectable.
    return element.closest?.(".message-content, .chat-control-line") || null;
  }
  return element?.closest?.(".message-content, .chat-control-line, #terminal-screen") || null;
}

function activeSourceSelection() {
  // A pending selection-menu timer is not by itself a selection: the browser
  // also fires selectionchange for a collapsed caret (it does while the page
  // initialises and when the terminal takes focus). Counting that timer as
  // "the human is selecting" made the first renderAll bail out at boot and
  // nothing repainted until the next poll, a second later. A real, non-
  // collapsed selection inside a source is still detected just below.
  if (state.pointerSelecting || state.pendingSelection !== null) return true;
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
    return {
      start, end, excerpt,
      kind: referenceKindForEvent(source),
      event_id: source.event_id,
      root, rect,
    };
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
  let menuLeft = left;
  if (top < gutter) {
    // Keep the toolbar away from the release point. A toolbar below a short
    // selection sits exactly where the next drag commonly starts.
    top = rect.top;
    menuLeft = rect.right + 10;
    if (menuLeft + menuRect.width > window.innerWidth - gutter) {
      menuLeft = rect.left - menuRect.width - 10;
    }
    if (menuLeft < gutter) {
      menuLeft = left;
      top = Math.min(window.innerHeight - menuRect.height - gutter, rect.bottom + 10);
    }
  }
  menu.style.left = `${Math.max(gutter, Math.min(window.innerWidth - menuRect.width - gutter, menuLeft))}px`;
  menu.style.top = `${Math.max(gutter, top)}px`;
}

function selectionIsHumanChat(candidate) {
  if (!candidate || candidate.kind !== "chat") return false;
  const source = state.timeline.find((event) => event.event_id === candidate.event_id);
  return source?.actor === "human";
}

function showSelectionMenu(candidate) {
  if (!candidate || !el.selection_menu || state.selectionBusy) return;
  state.pendingSelection = candidate;
  const terminal = ["completed", "failed", "interrupted"].includes(state.snapshot?.meeting?.status);
  el.selection_menu.querySelectorAll("[data-selection-action]").forEach((button) => {
    if (button.dataset.selectionAction === "add-to-conversation") {
      button.disabled = terminal;
      button.title = terminal ? "会话已结束，无法继续发消息" : "";
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




async function handleSelectionAction(action) {
  const selected = state.pendingSelection;
  if (!selected || state.selectionBusy) return;
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
    // The reference card above the composer already shows the quote; a toast
    // on top of it only covers the controls the user is about to use.
    if (!added) toast("这个引用已经添加", "info");
    return;
  }
}

async function loadHealth() {
  const health = await api("/api/health");
  state.health = health;
  const backendEntries = Object.values(health.backends || {});
  state.healthy = Boolean(health.core_ok && backendEntries.some(usableBackendEntry));
  const failedChecks = (health.checks || []).filter((item) => !item.ok);
  const visibleFailures = failedChecks.map((item) => {
    if (item.name === "model-api") return "请配置 model_api/config.toml 与 auth.json";
    if (item.name === "tmux") return "未找到 tmux";
    if (item.name === "vim") return "未找到 Vim";
    if (item.name === "codex-app-server") return `Codex 后端不可用：${item.detail}`;
    if (item.name === "trae-agent") return `Trae Agent 后端不可用：${item.detail}`;
    if (item.name === "claude-code") return `Claude Code 后端不可用：${item.detail}`;
    return "当前 Python 版本不受支持";
  });
  // 环境出问题时文字就写在下面那行里；一盏灯说不出是哪儿不对，所以不要了。
  el.health_message.textContent = visibleFailures.join("；");
  el.health_message.hidden = state.healthy;
  el.welcome_new_button.disabled = !state.healthy;
  syncBackendSelectors();
}

function backendEntry(backend, provider) {
  return state.health?.backends?.[`${backend}/${provider}`] || null;
}

// The app-server CLIs (codex, traecli) authenticate via the local CLI
// login, so they stay selectable even when their probe fails — the dialog
// then shows the real reason (e.g. "not logged in") instead of hiding the
// option.
const CLI_LOGIN_BACKENDS = new Set(["codex-app-server", "trae-agent"]);

function usableBackendEntry(item) {
  return item.ok || item.needs_api_key || CLI_LOGIN_BACKENDS.has(item.agent_backend);
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
  const isCliLogin = CLI_LOGIN_BACKENDS.has(el.backend_select.value);
  el.cli_default_model_row.hidden = !isCliLogin;
  const cliDefault = isCliLogin && el.cli_default_model.checked;
  el.model_input.disabled = cliDefault;
  if (cliDefault) {
    el.model_input.placeholder = el.backend_select.value === "trae-agent" ? "由 TraeCode CLI 决定" : "由 Codex CLI 决定";
  } else {
    const entry = backendEntry(el.backend_select.value, el.provider_select.value);
    el.model_input.placeholder = `默认 ${entry?.default_model || "由服务端决定"}`;
  }
}

function updateCredentialFields() {
  if (!el.api_key_input) return;
  const entry = backendEntry(el.backend_select.value, el.provider_select.value);
  const provider = el.provider_select.value;
  const backend = el.backend_select.value;
  const isCliLogin = CLI_LOGIN_BACKENDS.has(backend);
  if (el.api_credential_fields) el.api_credential_fields.hidden = isCliLogin;
  if (isCliLogin) {
    // These backends authenticate through the local CLI login; surface the
    // probe's real failure reason instead of asking for an API key.
    const cliName = backend === "trae-agent" ? "TraeCode CLI" : "Codex CLI";
    const loginCmd = backend === "trae-agent" ? "traecli login" : "codex login";
    el.api_key_hint.textContent = entry && !entry.ok
      ? `${cliName} 后端尚未就绪：${entry.detail || "未知原因"}。如未登录，请在终端运行 ${loginCmd}。`
      : `该组合使用本地 ${cliName} 登录，无需 API Key。`;
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

// 上次打开的是哪一场：刷新之后要回到它，而不是按"最近活动"跳到别的会上去。
// 私密模式下 localStorage 会抛，读不到就当没记过。
const SELECTED_MEETING_KEY = "remotebuddy.selected-meeting";

function rememberMeeting(meetingId) {
  try {
    window.localStorage.setItem(SELECTED_MEETING_KEY, String(meetingId || ""));
  } catch (_error) { /* 记不住也不影响用 */ }
}

function rememberedMeeting() {
  try {
    return window.localStorage.getItem(SELECTED_MEETING_KEY) || "";
  } catch (_error) {
    return "";
  }
}

// 列表按工作区分组，上次那场可能在屏幕外：刷新之后要能一眼看见它。
function revealSelectedMeeting() {
  el.meeting_list?.querySelector(".meeting-item.selected")?.scrollIntoView({ block: "nearest" });
}

async function loadMeetings() {
  const payload = await api("/api/meetings");
  state.meetings = payload.meetings || [];
  state.workspace = payload.workspace || "";
  state.workspaces = payload.workspaces || [];
  state.listed = payload.listed || [];
  state.workspaceTitles = payload.workspace_titles || {};
  state.meetingsRefreshedAt = Date.now();
  renderWorkspaceHint();
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
  setReferences([]);
  // 待发的图属于上一场会：换会议就丢掉，否则会混进另一场。
  setAttachments([]);
  state.selectedId = meetingId;
  state.snapshotEtag = "";
  state.eventsEtag = "";
  state.eventsEtagCursor = -1;
  // The member panel belongs to the meeting that was open: another meeting
  // opens with it closed.
  state.detailsAgent = null;
  state.agentOpHold = null;
  el.member_panel?.classList.remove("open");
  const [snapshot, eventPayload] = await Promise.all([
    api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`),
    api(`/api/meetings/${encodeURIComponent(meetingId)}/events?after=0`),
  ]);
  if (meetingId !== state.selectedId) return;
  rememberMeeting(meetingId);
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
  const [snapshotResult, eventsResult, ticketsResult] = await Promise.all([
    apiConditional(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`, state.snapshotEtag),
    apiConditional(`/api/meetings/${encodeURIComponent(meetingId)}/events?after=${cursor}`, eventsEtag),
    // 工单那一侧（这场会跑了流程才有）
    apiConditional(`/api/meetings/${encodeURIComponent(meetingId)}/tickets`, state.ticketsEtag)
      .catch(() => null),
  ]);
  if (meetingId !== state.selectedId) return;
  let changed = false;
  let additions = [];
  if (!snapshotResult.notModified) {
    state.snapshot = snapshotResult.payload;
    state.snapshotEtag = snapshotResult.etag;
    changed = true;
  }
  if (ticketsResult && !ticketsResult.notModified) {
    state.tickets = ticketsResult.payload.tickets || null;
    state.ticketsEtag = ticketsResult.etag;
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
  if (state.pointerSelecting || state.pendingSelection) return;
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
    } catch (error) {
      if (error.status === 404) {
        // The open meeting was deleted on disk: stop polling it instead of
        // toasting the same failure once a second, and let the sidebar stand.
        state.selectedId = null;
        state.snapshot = null;
        state.tickets = null;
        renderAll();
        return;
      }
      toast(error.message);
    }
    schedulePoll();
  }, active ? 500 : 1000);
}

// 交一张图：原始字节 POST 过去，服务把它存成这场会的一份文件，回名字和指纹。
async function uploadImage(file) {
  if (!state.selectedId) return;
  const answer = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/attachments`, {
    method: "POST",
    raw: true,
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  state.pendingAttachments.push(answer.attachment);
  renderAttachments();
}

async function uploadImages(files) {
  for (const file of files) {
    if (!file || !String(file.type || "").startsWith("image/")) continue;
    try {
      await uploadImage(file);
    } catch (error) {
      toast(error.message);
    }
  }
}

// 待发的图：缩略图 + 一个去掉它的按钮。
function renderAttachments() {
  if (!el.attachment_strip) return;
  el.attachment_strip.replaceChildren();
  const hasAttachments = state.pendingAttachments.length > 0;
  // The hidden attribute owns visibility; CSS supplies the strip layout only while it is present.
  el.attachment_strip.hidden = !hasAttachments;
  state.pendingAttachments.forEach((attachment, index) => {
    const item = node("div", "attachment-chip");
    const image = node("img", "attachment-thumb");
    image.src = attachmentUrl(attachment.name);
    image.alt = attachment.name;
    const remove = node("button", "attachment-remove");
    remove.type = "button";
    remove.title = "去掉这张图";
    remove.setAttribute("aria-label", remove.title);
    remove.append(closeCrossIcon());
    remove.addEventListener("click", () => {
      state.pendingAttachments.splice(index, 1);
      renderAttachments();
    });
    item.append(image, remove);
    el.attachment_strip.append(item);
  });
}

function setAttachments(list) {
  state.pendingAttachments = Array.isArray(list) ? list.slice() : [];
  renderAttachments();
}

// 一个圆里画出来的叉：字符 × 有基线，在圆里看着偏。
function closeCrossIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M6 6l12 12M18 6L6 18");
  svg.append(path);
  return svg;
}

// 点开一张图：弹一层盖住页面，点图以外的任何地方（或按 Esc）关掉。
function openImageLightbox(src, alt) {
  if (!el.image_lightbox || !el.image_lightbox_image) return;
  el.image_lightbox_image.src = src;
  el.image_lightbox_image.alt = alt || "";
  if (!el.image_lightbox.open) el.image_lightbox.showModal();
}

// 消息里的图：journal 只记名字，图本身按那条数据路径取。
function messageImages(event) {
  const data = event && event.data;
  const attachments = data && Array.isArray(data.attachments) ? data.attachments : [];
  const usable = attachments.filter((item) => item && typeof item.name === "string");
  if (!usable.length) return null;
  const holder = node("div", "message-images");
  for (const attachment of usable) {
    const src = attachmentUrl(attachment.name);
    const button = node("button", "message-image-button");
    button.type = "button";
    button.title = "点开看原图";
    const image = node("img", "message-image");
    image.src = src;
    image.alt = attachment.name;
    image.loading = "lazy";
    button.append(image);
    button.addEventListener("click", () => openImageLightbox(src, attachment.name));
    holder.append(button);
  }
  return holder;
}

function attachmentUrl(name) {
  return `/api/meetings/${encodeURIComponent(state.selectedId)}/attachments/${encodeURIComponent(name)}`;
}

async function postMeetingAction(suffix, body = {}) {
  if (!state.selectedId) return null;
  const answer = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/${suffix}`, { method: "POST", body: JSON.stringify(body) });
  await refreshSelected();
  await loadMeetings();
  return answer;
}

// 系统（Finder）的选目录框：浏览器自己的 showDirectoryPicker 拿不到绝对路径，
// 所以让服务端去问系统。取消就什么都不改。
async function chooseDirectory() {
  if (!el.choose_directory) return;
  el.choose_directory.disabled = true;
  try {
    const answer = await api("/api/choose-directory", {
      method: "POST",
      body: JSON.stringify({ directory: el.workspace_input.value.trim() }),
    });
    if (answer.directory) {
      el.workspace_input.value = answer.directory;
      state.workspaceFilled = true;
    }
  } catch (error) { toast(error.message); }
  finally { el.choose_directory.disabled = false; }
}

// 开始会话 = 打开一个工作区：选一个目录（系统选目录框），这场会就开在它里面，
// 第一句话在聊天框里说。取消就什么都不做。
async function openWorkspace() {
  const buttons = [el.welcome_new_button].filter(Boolean);
  buttons.forEach((button) => { button.disabled = true; });
  try {
    let directory = "";
    try {
      const answer = await api("/api/choose-directory", {
        method: "POST",
        body: JSON.stringify({ directory: state.workspace || "" }),
      });
      directory = answer.directory || "";
    } catch (error) {
      // 没有系统选目录框（不是 macOS）：退一步，让人打一条路径。
      toast(error.message);
      directory = (window.prompt("工作区目录", state.workspace || "") || "").trim();
    }
    if (!directory) return;                     // 取消了
    const created = await api("/api/meetings", {
      method: "POST",
      body: JSON.stringify({ workspace: directory }),
    });
    await loadMeetings();
    await selectMeeting(created.meeting_id);
  } catch (error) {
    toast(error.message);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

// One workspace row's own two actions: a session in it, and the menu that
// renames or forgets it. The name shown is the one it was given, else the
// directory's basename.
function groupActions(group) {
  const actions = node("div", "meeting-group-actions");
  const add = node("button", "meeting-group-action", "＋");
  add.type = "button";
  add.title = "在这个工作区里开一个新会话";
  add.setAttribute("aria-label", "新建会话");
  add.addEventListener("click", () => {
    newSessionInWorkspace(group.path).catch((error) => toast(error.message));
  });
  const more = node("button", "meeting-group-action", "···");
  more.type = "button";
  more.title = "重命名、删除工作区";
  more.setAttribute("aria-label", "工作区操作");
  more.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleWorkspaceMenu(group.path, more);
  });
  // 跟 DeepSeek 侧边栏一个次序：菜单在前，新建在后。
  actions.append(more, add);
  return actions;
}

// The "＋" on a workspace row: open a session in that directory, skipping the
// picker — the workspace is already chosen.
async function newSessionInWorkspace(path) {
  if (!path) return;
  const created = await api("/api/meetings", {
    method: "POST",
    body: JSON.stringify({ workspace: path }),
  });
  await loadMeetings();
  await selectMeeting(created.meeting_id);
}

function hideWorkspaceMenu() {
  if (!el.workspace_menu) return;
  el.workspace_menu.hidden = true;
  state.workspaceMenuPath = "";
}

function toggleWorkspaceMenu(path, anchor) {
  if (!el.workspace_menu || !path) return;
  if (!el.workspace_menu.hidden && state.workspaceMenuPath === path) {
    hideWorkspaceMenu();
    return;
  }
  state.workspaceMenuPath = path;
  const remove = el.workspace_menu.querySelector('[data-workspace-action="delete"]');
  if (remove) {
    // The default workspace is the project root: it is not on the list, so
    // taking it off is nothing, and it would be back on the next load. The
    // item is not shown rather than shown greyed out.
    remove.hidden = path === state.workspace;
  }
  el.workspace_menu.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const box = el.workspace_menu.getBoundingClientRect();
  const gutter = 8;
  const left = Math.max(gutter,
    Math.min(window.innerWidth - box.width - gutter, rect.right - box.width));
  const top = Math.max(gutter,
    Math.min(window.innerHeight - box.height - gutter, rect.bottom + 6));
  el.workspace_menu.style.left = `${left}px`;
  el.workspace_menu.style.top = `${top}px`;
}

// 会话那一行的「···」：浮层里就一件事——删除会话。它跟工作区那个菜单是一套长相。
function hideMeetingMenu() {
  if (!el.meeting_menu) return;
  el.meeting_menu.hidden = true;
  state.meetingMenuId = "";
}

function toggleMeetingMenu(meeting, anchor) {
  if (!el.meeting_menu || !meeting) return;
  if (!el.meeting_menu.hidden && state.meetingMenuId === meeting.meeting_id) {
    hideMeetingMenu();
    return;
  }
  state.meetingMenuId = meeting.meeting_id;
  el.meeting_menu.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const box = el.meeting_menu.getBoundingClientRect();
  const gutter = 8;
  const left = Math.max(gutter,
    Math.min(window.innerWidth - box.width - gutter, rect.right - box.width));
  const top = Math.max(gutter,
    Math.min(window.innerHeight - box.height - gutter, rect.bottom + 6));
  el.meeting_menu.style.left = `${left}px`;
  el.meeting_menu.style.top = `${top}px`;
}

// 删除会话：数据、终端、那一行一起走。删除按钮在浮层里，问一句再动手。
// 删除会话：跟重命名一样弹页面里的 modal，不用原生 confirm —— 原生框套在暗色界面外面，
// 标题、按钮、底色都是它自己的，跟这个工作区对不上。
function deleteMeeting(meetingId) {
  hideMeetingMenu();
  if (!el.delete_dialog) return;
  const summary = state.meetings.find((one) => one.meeting_id === meetingId);
  const name = summary ? meetingLabel(summary) : meetingId;
  state.deleteMeetingId = meetingId;
  el.delete_message.textContent = `确定要删除会话「${name}」吗？对话、终端和数据都会被删除。`;
  if (el.delete_error) { el.delete_error.hidden = true; el.delete_error.textContent = ""; }
  el.delete_dialog.showModal();
}

async function confirmDeleteMeeting() {
  const meetingId = state.deleteMeetingId;
  if (!meetingId) { el.delete_dialog?.close(); return; }
  el.confirm_delete.disabled = true;
  try {
    // 正文要带上（哪怕是个空对象）：这些接口读的是 JSON 正文，不带就是 400。
    await api(`/api/meetings/${encodeURIComponent(meetingId)}/delete`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  } catch (error) {
    // 删不掉要说清为什么，框留着让人重试（另一个服务在驱动这场会时就是这条）。
    if (el.delete_error) { el.delete_error.textContent = error.message || String(error); el.delete_error.hidden = false; }
    return;
  } finally {
    el.confirm_delete.disabled = false;
  }
  if (state.selectedId === meetingId) {
    // 删掉的正是打开着的那一场：回到"没开会"的样子，而不是继续轮询一个不存在的会。
    state.selectedId = null;
    state.snapshot = null;
    state.tickets = null;
    state.timeline = [];
    rememberMeeting("");
  }
  state.deleteMeetingId = "";
  el.delete_dialog?.close();
  await loadMeetings();
  renderAll();
}

// 重命名会话：弹页面里的 modal，不用原生 prompt——原生框套在暗色界面外面，标题、按钮、
// 底色都是它自己的，跟这个工作区对不上。
// 输入框不预填：没改过名时会话那一行的 label 就是首消息全文，预填等于把整段请求塞进去。
function renameMeeting(meetingId) {
  hideMeetingMenu();
  if (!el.rename_dialog) return;
  state.renameMeetingId = meetingId;
  if (el.rename_error) { el.rename_error.hidden = true; el.rename_error.textContent = ""; }
  el.rename_input.value = "";
  bindRenameDialog();
  el.rename_dialog.showModal();
  el.rename_input.focus();
}

// 弹框上那几颗只接一次线：dialog 是常驻元素，每开一次重接一遍监听会越叠越多。
function bindRenameDialog() {
  if (state.renameBound) return;
  state.renameBound = true;
  el.rename_close?.addEventListener("click", closeRenameDialog);
  el.rename_cancel?.addEventListener("click", closeRenameDialog);
  el.rename_form?.addEventListener("submit", (event) => {
    event.preventDefault();
    submitRename().catch((error) => toast(error.message));
  });
  el.rename_dialog?.addEventListener("close", () => { state.renameMeetingId = ""; });
}

function closeRenameDialog() {
  state.renameMeetingId = "";
  if (el.rename_dialog?.open) el.rename_dialog.close();
}

async function submitRename() {
  const meetingId = state.renameMeetingId;
  if (!meetingId) { closeRenameDialog(); return; }
  const name = (el.rename_input?.value || "").trim();
  if (!name) {
    if (el.rename_error) { el.rename_error.textContent = "名字不能是空的"; el.rename_error.hidden = false; }
    return;
  }
  try {
    await api(`/api/meetings/${encodeURIComponent(meetingId)}/rename`, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  } catch (error) {
    if (el.rename_error) { el.rename_error.textContent = error.message || String(error); el.rename_error.hidden = false; }
    return;
  }
  closeRenameDialog();
  await loadMeetings();
  renderAll();
}

// 拖动排序：鼠标放在会话行（或工作区标题）上**按住左键上下拖**——拖拽就是这个手势。
// 拖的过程中列表**不动**，只有一条白线标出落点；松手才把那一行插过去、发给服务。
// 左键要拖成拖、要选成选：没挪够 4px 之前什么都不动（单击打开、双击选词照旧），
// 真拖起来了才把刚开始的那点选字清掉。右键不参与，系统菜单照旧。
const DRAG_WORKSPACES = "workspaces";
// 手指抖一下不算拖：挪够这么多像素才开始。
const DRAG_THRESHOLD_PX = 4;

function rowNodesOf(workspacePath) {
  return [...el.meeting_list.querySelectorAll(".meeting-row")]
    .filter((row) => row.dataset.workspace === workspacePath);
}

function sectionNodes() {
  return [...el.meeting_list.querySelectorAll(".meeting-group")];
}

function sectionFor(workspacePath) {
  return sectionNodes().find((one) => one.dataset.workspace === workspacePath) || null;
}

function beginDrag(event, kind, node) {
  if (state.drag) return;
  if (event.button !== 0) return;
  // 这里不动 preventDefault：按下去什么都不发生，双击选词、单击打开都还照旧；
  // 真拖起来之后（updateDrag）才把刚开始的那点选字清掉。
  state.drag = {
    kind,
    node,
    key: kind === "meeting" ? node.dataset.meeting : node.dataset.workspace,
    scope: kind === "meeting" ? node.dataset.workspace : DRAG_WORKSPACES,
    originY: event.clientY,
    active: false,
    before: kind === "meeting"
      ? rowNodesOf(node.dataset.workspace).map((row) => row.dataset.meeting)
      : sectionNodes().map((one) => one.dataset.workspace),
  };
}

// 能插到谁旁边：会话只看它自己那个工作区里的行，工作区只看整段。所以会话出不了组，
// 工作区也不会插到另一个工作区的两条会话中间。
function dragCandidates(drag) {
  return drag.kind === "meeting" ? rowNodesOf(drag.scope) : sectionNodes();
}

function updateDrag(event) {
  const drag = state.drag;
  if (!drag) return;
  if (!drag.active) {
    if (Math.abs(event.clientY - drag.originY) < DRAG_THRESHOLD_PX) return;
    drag.active = true;
    drag.node.classList.add("dragging");
    // 到这里才抓住指针：松开时人可能在窗口外，没这一下就收不到 pointerup，这一枪会一直
    // 挂着。按下时就抓不行——那会把 click 也改投到这一行上，行里"打开会话"的按钮就收
    // 不到点击，等于点不动会话。
    try {
      drag.node.setPointerCapture?.(event.pointerId);
    } catch (_error) { /* 抓不住也照旧 */ }
    // 鼠标动这几下可能已经拉出一点选区了：拖起来了就把它清掉，免得半行字亮着。
    clearBrowserSelection();
  }
  // 被拖的那个不参与比较，剩下的按"指针越过谁的中线"决定插到第几位。这里只算、不挪：
  // 拖的时候列表不动，只有白线在走（落位在 finishDrag 里一次做完）。
  const others = dragCandidates(drag).filter((one) => one !== drag.node);
  let index = others.length;
  for (let position = 0; position < others.length; position += 1) {
    const box = others[position].getBoundingClientRect();
    if (event.clientY < box.top + box.height / 2) { index = position; break; }
  }
  drag.index = index;
  showDropLine(others, index, drag);
}

// 目的位置那条白线：画在"会插到谁前面"的那条边；插在最后就画在最后一条的下边。
function showDropLine(others, index, drag) {
  if (!el.drop_line) return;
  const box = others.length
    ? (others[index] || others[others.length - 1]).getBoundingClientRect()
    : drag.node.getBoundingClientRect();
  const atEnd = !others.length || index >= others.length;
  const listBox = el.meeting_list.getBoundingClientRect();
  el.drop_line.style.left = `${Math.round(listBox.left + 4)}px`;
  el.drop_line.style.width = `${Math.round(Math.max(0, listBox.width - 8))}px`;
  el.drop_line.style.top = `${Math.round(atEnd ? box.bottom : box.top)}px`;
  el.drop_line.hidden = false;
}

function hideDropLine() {
  if (el.drop_line) el.drop_line.hidden = true;
}

// 松手：到这里才动列表——把那一行插到白线画的地方——顺序真变了才发一次，再和服务对一遍。
async function finishDrag() {
  const drag = state.drag;
  state.drag = null;
  hideDropLine();
  if (!drag) return;
  drag.node.classList.remove("dragging");
  if (!drag.active || drag.index < 0) return;
  const others = dragCandidates(drag).filter((one) => one !== drag.node);
  const next = others.slice();
  const insert = Math.max(0, Math.min(next.length, drag.index));
  next.splice(insert, 0, drag.node);
  const keys = next.map(
    (one) => (drag.kind === "meeting" ? one.dataset.meeting : one.dataset.workspace));
  if (keys.join("|") === drag.before.join("|")) return;    // 落点就是原位：不动
  const anchor = others[insert] || null;
  if (anchor) anchor.parentNode.insertBefore(drag.node, anchor);
  else if (drag.kind === "meeting") sectionFor(drag.scope)?.append(drag.node);
  else el.meeting_list.append(drag.node);
  // 拖完之后浏览器还会补一个 click：刚拖过这一行的人不是想打开它。
  state.suppressClickUntil = Date.now() + 400;
  // 先按新顺序把本地那份也改了：轮询正好在这时候重画，才不会又画回旧顺序。
  if (drag.kind === "meeting") orderLocalMeetings(drag.scope, keys);
  else state.workspaces = keys.slice();
  await api(
    drag.kind === "meeting" ? "/api/meetings/order" : "/api/workspaces/order",
    {
      method: "POST",
      body: JSON.stringify(drag.kind === "meeting"
        ? { workspace: drag.scope, meetings: keys }
        : { workspaces: keys }),
    },
  );
  await loadMeetings();
}

function orderLocalMeetings(workspacePath, keys) {
  const mine = new Map(state.meetings
    .filter((one) => (one.workspace_root || "") === workspacePath)
    .map((one) => [one.meeting_id, one]));
  const ordered = keys.map((key) => mine.get(key)).filter(Boolean);
  let next = 0;
  state.meetings = state.meetings.map((one) => (
    (one.workspace_root || "") === workspacePath ? (ordered[next++] || one) : one));
}

// 重命名改的是列表里怎么叫它：目录名不动，会议一条也不挪。留空就用目录名。
async function renameWorkspace(path) {
  hideWorkspaceMenu();
  if (!path) return;
  const current = state.workspaceTitles[path] || basename(path);
  const answer = window.prompt("这个工作区叫什么（留空就用目录名）", current);
  if (answer === null) return;
  await api("/api/workspaces/rename", {
    method: "POST",
    body: JSON.stringify({ directory: path, title: answer.trim() }),
  });
  await loadMeetings();
}

// 删除只把它从名单上拿掉：目录和里面的会议都不动，再加一次就回来了。
async function deleteWorkspace(path) {
  hideWorkspaceMenu();
  if (!path) return;
  const name = state.workspaceTitles[path] || basename(path);
  if (!window.confirm(`把「${name}」从工作区列表里去掉？目录和里面的会议都不动。`)) return;
  await api("/api/workspaces/delete", {
    method: "POST",
    body: JSON.stringify({ directory: path }),
  });
  await loadMeetings();
}

// The "＋" beside 工作区 in the sidebar: remember a directory as a workspace
// without opening a meeting — a workspace can be there before its first
// session. The directory comes from the same system picker; cancel changes
// nothing, and no meeting means no backend or key setup.
async function addWorkspace() {
  if (!el.add_workspace_button) return;
  el.add_workspace_button.disabled = true;
  try {
    let directory = "";
    // 加的是"另一个项目"，所以对话框从服务自己那个目录的上一层打开：那一层通常就是
    // 放各个项目的地方。停在项目自己身上，一点就选中它，等于没加。
    const startAt = state.workspace.replace(/\/[^/]+$/, "") || "/";
    try {
      const answer = await api("/api/choose-directory", {
        method: "POST",
        body: JSON.stringify({ directory: startAt }),
      });
      directory = answer.directory || "";
    } catch (error) {
      // No system picker (not macOS): fall back to typing a path.
      toast(error.message);
      directory = (window.prompt("工作区目录", startAt) || "").trim();
    }
    if (!directory) return;                     // the picker was cancelled
    const answer = await api("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ directory }),
    });
    // 已经在名单上（或者就是默认那一个）时说一句：不然"点了没反应"看着像坏了。
    if (answer.added === false) toast(`${basename(directory)} 已经在工作区列表里`, "info");
    await loadMeetings();
  } catch (error) {
    toast(error.message);
  } finally {
    el.add_workspace_button.disabled = false;
  }
}

function openNewDialog() {
  el.new_error.hidden = true;
  el.api_key_input.value = "";
  el.base_url_input.value = "";
  syncBackendSelectors(false);
  el.new_dialog.showModal();
  el.initial_message.focus();
}

function bind() {
  el.add_workspace_button?.addEventListener("click", addWorkspace);
  el.workspace_menu?.querySelector('[data-workspace-action="rename"]')?.addEventListener("click", () => {
    renameWorkspace(state.workspaceMenuPath).catch((error) => toast(error.message));
  });
  el.workspace_menu?.querySelector('[data-workspace-action="delete"]')?.addEventListener("click", () => {
    deleteWorkspace(state.workspaceMenuPath).catch((error) => toast(error.message));
  });
  el.meeting_menu?.querySelector('[data-meeting-action="rename"]')?.addEventListener("click", () => {
    renameMeeting(state.meetingMenuId).catch((error) => toast(error.message));
  });
  el.meeting_menu?.querySelector('[data-meeting-action="delete"]')?.addEventListener("click", () => {
    deleteMeeting(state.meetingMenuId).catch((error) => toast(error.message));
  });
  el.close_delete_dialog?.addEventListener("click", () => { state.deleteMeetingId = ""; el.delete_dialog?.close(); });
  el.cancel_delete?.addEventListener("click", () => { state.deleteMeetingId = ""; el.delete_dialog?.close(); });
  el.delete_form?.addEventListener("submit", (event) => {
    event.preventDefault();
    confirmDeleteMeeting().catch((error) => toast(error.message));
  });
  el.delete_dialog?.addEventListener("close", () => { state.deleteMeetingId = ""; });
  el.welcome_new_button.addEventListener("click", openWorkspace);
  el.welcome_advanced_button?.addEventListener("click", openNewDialog);
  const railMore = document.getElementById("rail-more-button");
  const appShell = document.querySelector(".app-shell");
  if (railMore && appShell) {
    railMore.addEventListener("click", () => appShell.classList.toggle("sidebar-open"));
  }
  document.querySelector(".workspace-main")?.addEventListener("pointerdown", () => {
    appShell?.classList.remove("sidebar-open");
  });
  el.close_dialog.addEventListener("click", () => el.new_dialog.close());
  el.cancel_new.addEventListener("click", () => el.new_dialog.close());
  el.choose_directory?.addEventListener("click", () => chooseDirectory());
  el.backend_select.addEventListener("change", () => syncBackendSelectors(true));
  el.provider_select.addEventListener("change", () => syncBackendSelectors(true));
  el.cli_default_model?.addEventListener("change", () => syncCliDefaultModel());
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
      const cliDefault = CLI_LOGIN_BACKENDS.has(el.backend_select.value) && el.cli_default_model?.checked;
      const response = await api("/api/meetings", {
        method: "POST",
        body: JSON.stringify({
          message: el.initial_message.value,
          agent_backend: el.backend_select.value,
          agent_provider: el.provider_select.value,
          model: cliDefault ? "" : (el.model_input.value.trim() || null),
          typing_profile: typingProfile,
          ...(el.workspace_input?.value.trim() ? { workspace: el.workspace_input.value.trim() } : {}),
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
  el.attach_image_button?.addEventListener("click", () => el.image_input?.click());
  // 只有点在图上才留着；点图以外的任何地方（包括那层黑底）都关掉。
  el.image_lightbox?.addEventListener("click", (event) => {
    if (event.target === el.image_lightbox) el.image_lightbox.close();
  });
  // 也可以直接点右上角那个圆圈：不知道"点外围"的人也有个去处。
  el.image_lightbox_close?.addEventListener("click", () => el.image_lightbox.close());
  el.member_panel_close?.addEventListener("click", () => closeMemberDetails());
  el.image_input?.addEventListener("change", async () => {
    const files = Array.from(el.image_input.files || []);
    el.image_input.value = "";                       // 同一张图可以再选一次
    await uploadImages(files);
  });
  // 粘贴图片：截图之后直接按 ⌘V 就进来了。
  el.message_input.addEventListener("paste", (event) => {
    const items = Array.from((event.clipboardData && event.clipboardData.items) || []);
    const files = items.filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    event.preventDefault();
    uploadImages(files);
  });
  // 拖到页面上的图片：阻止浏览器打开文件，只把图片交给同一条上传路径。
  document.addEventListener("dragover", (event) => {
    if (Array.from(event.dataTransfer?.items || []).some((item) => item.kind === "file" && item.type.startsWith("image/"))) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    }
  });
  document.addEventListener("drop", (event) => {
    const droppedFiles = Array.from(event.dataTransfer?.files || []);
    if (!droppedFiles.length) return;
    event.preventDefault();
    const files = droppedFiles.filter((file) => String(file.type || "").startsWith("image/"));
    if (!files.length) return;
    uploadImages(files);
  });
  el.message_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = el.message_input.value.trim();
    const attachments = state.pendingAttachments.slice();
    if (!message && !attachments.length) return;
    el.send_message_button.disabled = true;
    // Sending a message is a live action: leave playback/free-scroll mode
    // so the user's own text and the agent's next response are visible.
    state.followLive = true;
    state.chatFollow = true;
    state.playbackIndex = state.timeline.length - 1;
    // "interrupt" == the old 立即插入 choice, now the only behavior: the
    // message reaches the agent right away instead of waiting for its next
    // action boundary.
    const payload = { message, delivery: "interrupt" };
    const references = state.references;
    if (references.length) payload.references = references;
    if (attachments.length) payload.attachments = attachments.map((one) => ({ name: one.name }));
    // Empty the box the moment the human sends, not when the server answers:
    // the request can take a while and the text already left the composer.
    el.message_input.value = "";
    growMessageInput();
    setReferences([]);
    setAttachments([]);
    try {
      const answer = await postMeetingAction("messages", payload);
      if (answer && answer.agent_error) {
        // 话已经收下了，只是 Agent 没能起来（配置读不出来之类）。不把文字塞回输入框 ——
        // 人会以为没发出去，再发一遍，真会里就这么重复了三条。原因说一句，会议状态里也有。
        toast(`消息收到了，但 Agent 没能起来：${answer.agent_error}`);
      }
    }
    catch (error) {
      // Only hand the text back when the request was actually refused. A 5xx (or a
      // dropped connection) can happen after the message was already stored, and
      // putting it back in the box makes the human send it twice.
      const refused = !error.status || (error.status >= 400 && error.status < 500);
      if (refused) {
        el.message_input.value = message;
        growMessageInput();
        setReferences(references);
        setAttachments(attachments);
      }
      toast(error.message);
    }
    finally { el.send_message_button.disabled = false; }
  });
  // The composer box has no resize grip (TRAE style): grow it with the text up
  // to the CSS max-height, then scroll inside.
  const growMessageInput = () => {
    el.message_input.style.height = "auto";
    el.message_input.style.height = `${el.message_input.scrollHeight}px`;
  };
  el.message_input.addEventListener("input", growMessageInput);
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
      toast("推理力度已更新，下一个工作周期生效", "success");
    } catch (error) { toast(error.message); }
  });
  // 更新：只换 Skill 文本，进程不动。指令按请求传的 harness 下一句就用上；
  // 把指令写在自己进程里的（Claude Code / Codex）要等下一次开工。
  el.reload_skill_button?.addEventListener("click", async () => {
    if (!state.selectedId) return;
    el.reload_skill_button.disabled = true;
    try {
      const answer = await api(
        `/api/meetings/${encodeURIComponent(state.selectedId)}/skill/reload`,
        { method: "POST", body: "{}" });
      if (!answer.changed) toast("Skill 没变", "info");
      else if (!answer.agent_running) toast("Skill 已更新：没有在跑的进程，下一轮开工就用新的", "success");
      else if (answer.applies_to_next_request) toast("Skill 已更新：下一句就用新的", "success");
      else toast("Skill 已更新：这个后端把指令写在进程里，下次开工生效；要立刻生效就点\"重启\"", "success");
      if (answer.changed) holdMemberState("updating");
      closeMemberDetails();          // 按完就收起来，跟重启一样：面板挡着终端
      await refreshSelected();
      await loadMeetings();
    } catch (error) { toast(error.message); }
    finally { el.reload_skill_button.disabled = false; }
  });
  // 重启：把 agent 进程杀掉，用现在的 Skill 重开一个；同一轮继续，历史照给。
  el.restart_agent_button?.addEventListener("click", async () => {
    if (!state.selectedId) return;
    if (!confirm("杀掉当前 agent 进程，用最新的 Skill 重开一个？这一轮会从进程启动重新开始。")) return;
    el.restart_agent_button.disabled = true;
    try {
      const answer = await api(
        `/api/meetings/${encodeURIComponent(state.selectedId)}/agent/restart`,
        { method: "POST", body: "{}" });
      toast(answer.started ? "没有在跑的进程，已经用新 Skill 起了一个" : "已重启，进程马上换一个", "info");
      holdMemberState("restarting");
      closeMemberDetails();          // 按完就收起来：面板挡着终端，没别的用处了
      await refreshSelected();
      await loadMeetings();
    } catch (error) { toast(error.message); }
    finally { el.restart_agent_button.disabled = false; }
  });
  el.acquire_button.addEventListener("click", () => postMeetingAction("control/acquire").catch((error) => toast(error.message)));
  el.release_button.addEventListener("click", () => postMeetingAction("control/release").catch((error) => toast(error.message)));
  el.terminal_copy_button?.addEventListener("click", () => copyTextToClipboard(lastTerminalPlainText));
  el.terminal_copy_ansi_button?.addEventListener("click", () => copyTextToClipboard(lastTerminalAnsiText || lastTerminalPlainText));
  // Refit the terminal when the window/panel size changes (DevTools dock,
  // pane resize), so rows are never clipped and the cursor stays visible.
  window.addEventListener("resize", () => refitTerminal());
  // Layout changes can resize the terminal without resizing the window.
  if (typeof ResizeObserver === "function" && el.terminal_screen) {
    const terminalResizeObserver = new ResizeObserver(() => refitTerminal());
    terminalResizeObserver.observe(el.terminal_screen);
  }
  el.resume_button.addEventListener("click", () => postMeetingAction("agent/resume").catch((error) => toast(error.message)));
  el.complete_button.addEventListener("click", () => { if (confirm("结束会话并保留 tmux 工作区？")) postMeetingAction("complete").catch((error) => toast(error.message)); });
  el.stop_button.addEventListener("click", () => { if (confirm("中止会话并清理 tmux 工作区？")) postMeetingAction("stop").catch((error) => toast(error.message)); });
  el.cleanup_button.addEventListener("click", () => postMeetingAction("cleanup").catch((error) => toast(error.message)));
  el.ticket_issue_button.addEventListener("click", () => postTicket(el.ticket_issue_button.dataset.ticket));
  el.ticket_toggle.addEventListener("click", () => {
    el.session_view.classList.toggle("tickets-open");
  });
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
  // Release anywhere (even outside the containers) ends the drag; refresh
  // the selection once so the menu appears exactly at mouse release.
  document.addEventListener("pointerup", (event) => {
    if (!state.pointerSelecting) return;
    state.pointerSelecting = false;
    scheduleSelectionMenu(event.detail || 1);
  });
  document.addEventListener("pointercancel", () => { state.pointerSelecting = false; });
  document.addEventListener("selectionchange", scheduleSelectionMenu);
  // 浮层是 chip 弹出来的：点别处（chip 和浮层本身以外）就收回去。走捕获阶段，
  // 终端和别处那些 pointerdown 处理吞不掉它，不然面板会关不上。
  document.addEventListener("pointerdown", (event) => {
    if (!state.detailsAgent) return;
    if (event.target.closest?.(".agent-state") || event.target.closest?.("#member-panel")) return;
    closeMemberDetails();
  }, true);
  document.addEventListener("pointerdown", (event) => {
    if (el.workspace_menu && !el.workspace_menu.hidden
        && !el.workspace_menu.contains(event.target)) {
      hideWorkspaceMenu();
    }
    if (el.meeting_menu && !el.meeting_menu.hidden
        && !el.meeting_menu.contains(event.target)) {
      hideMeetingMenu();
    }
    if (el.selection_menu.contains(event.target)) return;
    hideSelectionMenu();
    // A terminal selection lives inside xterm rather than the DOM, so it
    // survives this click: focus returns to the terminal, the selection-change
    // re-check still finds that selection and the menu pops straight back. Any
    // click outside the terminal is a dismissal, so drop the selection too.
    const inTerminal = el.terminal_screen
      && (event.target === el.terminal_screen || el.terminal_screen.contains(event.target));
    if (!inTerminal && terminalEngine) {
      try {
        terminalEngine.term?.clearSelection?.();
      } catch (_error) {
        // Clearing a selection must never break the click that asked for it.
      }
    }
    // Do not clear the browser selection when the user is clicking inside a
    // selectable source. Double/triple-click selection depends on the browser
    // preserving and extending the previous selection across rapid clicks.
    //
    // The chrome outside the workspace (sidebar, dialogs) is exempt too:
    // clearing the ranges inside the mousedown kills the native selection
    // gesture before it can start, which is why meeting ids, the brand and the
    // dialog notes could not be drag-selected or copied at all.
    const chromeSurface = event.target.closest
      && (event.target.closest(".sidebar") || event.target.closest(".rail")
          || event.target.closest("dialog"));
    if (!sourceRootFromNode(event.target) && !chromeSurface) {
      clearBrowserSelection();
    }
  });
  // 右键拖顺序：按住挪（画白线）、松手落位。右键点一下不动顺序。
  document.addEventListener("pointermove", updateDrag);
  document.addEventListener("pointerup", () => {
    if (state.drag) finishDrag().catch((error) => toast(error.message));
  });
  document.addEventListener("pointercancel", () => {
    if (state.drag) finishDrag().catch((error) => toast(error.message));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeMemberDetails();
      hideSelectionMenu();
      hideWorkspaceMenu();
      hideMeetingMenu();
      clearBrowserSelection();
      state.keyboardSelection = null;
    }
  });
  // While the user is watching the terminal (live or takeover), clicking
  // anywhere outside it -- including non-focusable surfaces like chat blank
  // space, where no focus event ever fires -- moves focus to <body> and stops
  // the cursor blink. After such a click, hand focus straight back to the
  // terminal; editable fields (chat composer, dialogs) are never touched, and
  // playback scrubbing (followLive false) leaves focus alone.
  //
  // Only the workspace does this. The sidebar and the dialogs live outside it
  // and are ordinary chrome whose text has to stay selectable: grabbing focus
  // on their pointerdown collapsed the selection the human had just started,
  // so meeting ids and dialog notes could not be copied at all.
  // The first rail button belongs to the header row, so it is centred on the
  // header instead of on a guessed padding: the header grows past its
  // min-height as soon as a member chip wraps or a failure message appears, and
  // that depends on the window width.
  const sessionHeader = document.querySelector(".session-header");
  const railButton = document.querySelector(".rail-button");
  if (sessionHeader && railButton) {
    const syncRailPadding = () => {
      const headerHeight = sessionHeader.offsetHeight;
      // No meeting open, or a narrow window where the rail and the header are
      // stacked instead of side by side: fall back to the stylesheet's default.
      if (!headerHeight || getComputedStyle(appShell).display !== "grid") {
        document.documentElement.style.removeProperty("--rail-top-padding");
        return;
      }
      const offset = Math.max(0, (headerHeight - railButton.offsetHeight) / 2);
      document.documentElement.style.setProperty("--rail-top-padding", `${offset}px`);
    };
    syncRailPadding();
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(syncRailPadding);
      observer.observe(sessionHeader);
      observer.observe(railButton);
    }
    window.addEventListener("resize", syncRailPadding);
  }
  const workspaceMain = document.querySelector(".workspace-main");
  // Selectable-text surfaces inside the workspace must never lose focus to the
  // terminal either: stealing focus out from under a mousedown on chat text
  // (or the header identity) also cancels native text selection before it can
  // start (drag, double-click and Shift+arrows all die; copy returns nothing).
  function isTextSelectionSurface(node) {
    const selectors = [".message-content", ".chat-control-line", ".session-identity",
                       ".presence-strip"];
    for (const selector of selectors) {
      if (node && node.closest && node.closest(selector)) return true;
    }
    return false;
  }
  document.addEventListener("pointerdown", (event) => {
    if (!(takeoverStream.active || state.followLive)) return;
    const target = event.target;
    if (!workspaceMain || !workspaceMain.contains(target)) return;
    if (el.terminal_screen && (target === el.terminal_screen || el.terminal_screen.contains(target))) return;
    // The selection toolbar drives the selection the user just made: moving
    // focus to the terminal on its own pointerdown wipes that selection before
    // the click lands, so 添加到对话 silently did nothing -- but only for chat
    // text, since xterm keeps its selection internally.
    if (el.selection_menu && el.selection_menu.contains(target)) return;
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
    hidePlaybackHover();
    // Moving the view on purpose releases a clicked citation: otherwise the
    // terminal stays pinned to the frame that quote pointed at.
    state.highlightReference = null;
    markPlaybackFrozen();
    state.followLive = false;
    state.chatFollow = true;
    state.playbackIndex = Number(el.playback_slider.value);
    stopLiveScreenQueue();
    stopPlayback();
    renderAll();
  });
  el.live_button.addEventListener("click", () => {
    hidePlaybackHover();
    state.highlightReference = null;      // back to live: the citation is released
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
    state.highlightReference = null;      // playing moves the view: release the pin
    if (state.playing) { stopPlayback(); return; }
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

let assetVersion = "";

async function watchAssetChanges() {
  while (true) {
    try {
      const response = await fetch(`/api/assets/changes?version=${encodeURIComponent(assetVersion)}`, { cache: "no-store" });
      if (!response.ok) return;
      const answer = await response.json();
      if (!assetVersion) {
        assetVersion = answer.version;
        continue;
      }
      const [oldCss, oldJs] = assetVersion.split(":");
      if (answer.assets["workspace.css"] !== oldCss) {
        const link = document.querySelector('link[rel="stylesheet"][href*="workspace.css"]');
        if (link) {
          const replacement = document.createElement("link");
          replacement.rel = "stylesheet";
          replacement.href = `/assets/workspace.css?v=${encodeURIComponent(answer.assets["workspace.css"])}`;
          replacement.onload = () => link.remove();
          link.after(replacement);
        }
      }
      if (answer.assets["workspace.js"] !== oldJs
          && document.visibilityState === "hidden"
          && !isEditableFocusTarget(document.activeElement)) {
        location.reload();
        return;
      }
      assetVersion = answer.version;
    } catch (_error) {
      return;
    }
  }
}

watchAssetChanges();

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
    // The health probe talks to the CLI backends (traecli handshakes take
    // seconds) and only gates the new-session button, so it must never hold
    // up opening a meeting: start it and let it land whenever it lands.
    loadHealth().catch((error) => toast(error.message));
    await loadMeetings();
    // 刷新回到上次打开的那一场（存在浏览器里）；它要是没了，才退回"最近活动的第一场"。
    // 一场都没有的时候欢迎卡片留着，人来选工作区。
    const known = new Set(state.meetings.map((one) => one.meeting_id));
    const remembered = rememberedMeeting();
    const target = known.has(remembered)
      ? remembered
      : (state.meetings.length ? state.meetings[0].meeting_id : "");
    if (target) {
      await selectMeeting(target);
      revealSelectedMeeting();
    } else {
      renderAll();
    }
  } catch (error) {
    // Only a genuine failure (service down, meeting unreadable) leaves the
    // welcome card on screen.
    toast(error.message);
    renderAll();
  }
  setInterval(() => { if (state.snapshot) renderHeader(); }, 1000);
}

boot();
