"use strict";

const LIVE_SCREEN_FRAME_MS = 700;
const MAX_LIVE_SCREEN_QUEUE = 120;

const el = Object.fromEntries([
  "health-dot", "health-message", "new-button", "welcome-new-button", "meeting-count", "meeting-list", "list-empty",
  "welcome", "session-view", "meeting-title", "status-badge", "owner-badge", "session-meta",
  "phase-label", "backend-label", "model-label", "typing-profile-label", "action-label", "runtime-label", "event-count", "failure-message", "refresh-button",
  "resume-button", "complete-button", "cleanup-button", "stop-button", "chat-list", "message-form",
  "message-input", "send-message-button", "message-hint", "delivery-mode", "reference-card", "terminal-command", "terminal-path",
  "terminal-screen", "typing-state", "terminal-readonly", "control-label", "acquire-button", "release-button", "terminal-controls",
  "command-form", "command-select", "command-path", "vim-form", "vim-text", "process-input-label", "play-button",
  "playback-slider", "playback-position", "live-button", "inspect-button", "screen-reference-button", "free-scroll-button", "follow-scroll-button", "new-dialog", "new-form",
  "close-dialog", "cancel-new", "initial-message", "model-input", "start-button", "new-error",
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
  reference: null,
  highlightReference: null,
  selectingScreen: false,
};

const labels = {
  status: { created: "已创建", starting: "启动中", running: "协作中", completed: "已完成", failed: "失败", interrupted: "已中止" },
  phase: { created: "等待启动", starting: "正在打开终端", agent_thinking: "Agent 思考中", agent_acting: "Agent 操作中", waiting_for_user: "等待你的消息", completion_proposed: "Agent 提议完成", human_control: "由你控制", api_error: "模型连接中断", cycle_timeout: "本轮已超时", completed: "已结束", failed: "执行失败", interrupted: "已中止" },
  owner: { agent: "Agent 控制", human: "你在控制", none: "无人控制" },
  message: { request: "请求", ack: "回应", progress: "进度", question: "问题", final: "结果" },
  typing: { demo: "演示", balanced: "平衡", fast: "快速" },
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

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if ((options.method || "GET") !== "GET") headers.set(state.csrfHeader, state.csrfToken);
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const text = await response.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch (_error) { payload = { error: text }; }
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

function eventsAtPlayback() {
  if (!state.timeline.length) return [];
  const end = state.followLive ? state.timeline.length - 1 : state.playbackIndex;
  return state.timeline.slice(0, end + 1);
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
    button.append(top, node("span", "meeting-item-meta", `${labels.phase[meeting.phase] || meeting.phase} · ${fmtTime(meeting.updated_at)}`));
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
  el.session_meta.textContent = `${meeting.session_name} · ${meeting.session_state}`;
  const failure = typeof meeting.failure === "string" ? meeting.failure.trim() : "";
  el.failure_message.textContent = failure;
  el.failure_message.hidden = !failure || !["api_error", "cycle_timeout", "failed"].includes(meeting.phase);
  el.phase_label.textContent = labels.phase[meeting.phase] || meeting.phase;
  el.backend_label.textContent = meeting.agent_backend || "responses";
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
  el.message_input.disabled = terminalState;
  el.send_message_button.disabled = terminalState;
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

function renderChat() {
  const previousScrollTop = el.chat_list.scrollTop;
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
      const body = node("p", "message-content", event.content);
      article.append(head, body);
      const reference = event.data?.reference;
      if (reference?.excerpt) {
        const quote = node("blockquote", "message-reference", `引用 · ${reference.excerpt}`);
        quote.title = "跳转到引用来源";
        quote.addEventListener("click", () => {
          const index = state.timeline.findIndex((item) => item.event_id === reference.event_id);
          if (index < 0) return;
          state.followLive = false;
          state.playbackIndex = index;
          state.highlightReference = reference;
          stopLiveScreenQueue();
          stopPlayback();
          renderAll();
          const activeQuote = [...el.chat_list.querySelectorAll(".message-reference")]
            .find((item) => item.closest("article")?.dataset.eventId === event.event_id);
          activeQuote?.classList.add("highlighted");
          setTimeout(() => activeQuote?.classList.remove("highlighted"), 900);
        });
        if (state.highlightReference?.event_id === reference.event_id) quote.classList.add("highlighted");
        article.append(quote);
      }
      el.chat_list.append(article);
    }
    if (!chats.length) el.chat_list.append(node("div", "empty-chat", "这个回放位置还没有聊天消息"));
    if (state.chatFollow) {
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
  const latest = state.followLive && state.liveScreenEvent
    ? state.liveScreenEvent
    : screens.at(-1);
  const terminal = latest?.data || state.snapshot?.latest_screen?.data || {};
  el.terminal_command.textContent = terminal.current_command || "shell";
  el.terminal_path.textContent = terminal.current_path
    ? terminal.current_path.split("/").slice(-2).join("/")
    : "workspace";
  el.terminal_screen.textContent = latest?.content || "等待终端画面…";
  el.terminal_screen.classList.toggle(
    "reference-target",
    Boolean(state.highlightReference?.kind === "screen" && latest?.event_id === state.highlightReference.event_id),
  );
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

function renderPlayback() {
  const max = state.timeline.length - 1;
  el.playback_slider.max = String(max);
  el.playback_slider.min = "-1";
  el.playback_slider.value = String(state.followLive ? max : state.playbackIndex);
  el.playback_position.textContent = state.followLive ? `实时 · ${state.timeline.length}` : `${state.playbackIndex + 1} / ${state.timeline.length}`;
  el.live_button.disabled = state.followLive;
  el.play_button.textContent = state.playing ? "❚❚" : "▶";
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

function setReference(reference) {
  state.reference = reference;
  if (!reference) {
    el.reference_card.hidden = true;
    el.reference_card.replaceChildren();
    return;
  }
  el.reference_card.hidden = false;
  el.reference_card.replaceChildren(
    node("span", "reference-kind", reference.kind === "screen" ? "Screen 引用" : "Chat 引用"),
    node("span", "reference-excerpt", reference.excerpt || ""),
  );
  const remove = node("button", "icon-button", "×");
  remove.type = "button";
  remove.title = "移除引用";
  remove.addEventListener("click", () => setReference(null));
  el.reference_card.append(remove);
}

function currentScreenEvent() {
  const screens = eventsAtPlayback().filter((event) => event.kind === "screen");
  return (state.followLive && state.liveScreenEvent) || screens.at(-1) || null;
}

function selectionOffsets(root, selection) {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const before = range.cloneRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length;
  const end = start + range.toString().length;
  const content = root.textContent || "";
  if (start < 0 || end > content.length || end <= start) return null;
  return { start, end, excerpt: content.slice(start, end) };
}

function captureScreenTextSelection() {
  if (!state.selectingScreen) return;
  const source = currentScreenEvent();
  const selected = selectionOffsets(el.terminal_screen, window.getSelection());
  if (!source || !selected || !selected.excerpt.trim()) return;
  setReference({
    kind: "screen",
    event_id: source.event_id,
    region: { start: selected.start, end: selected.end },
    excerpt: selected.excerpt,
  });
}

async function loadHealth() {
  const health = await api("/api/health");
  state.healthy = Boolean(health.ok);
  el.health_dot.classList.toggle("ok", state.healthy);
  const failedChecks = (health.checks || []).filter((item) => !item.ok);
  const failed = failedChecks.map((item) => item.detail);
  const visibleFailures = failedChecks.map((item) => {
    if (item.name === "model-api") return "请配置 model_api/config.toml 与 auth.json";
    if (item.name === "tmux") return "未找到 tmux";
    if (item.name === "vim") return "未找到 Vim";
    if (item.name === "codex-app-server") return `Codex 后端不可用：${item.detail}`;
    return "当前 Python 版本不受支持";
  });
  el.health_dot.title = state.healthy ? "运行环境就绪" : failed.join("\n");
  el.health_message.textContent = visibleFailures.join("；");
  el.health_message.hidden = state.healthy;
  el.new_button.disabled = !state.healthy;
  el.welcome_new_button.disabled = !state.healthy;
}

async function loadMeetings() {
  const payload = await api("/api/meetings");
  state.meetings = payload.meetings || [];
  renderMeetingList();
}

async function selectMeeting(meetingId) {
  stopLiveScreenQueue();
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
  if (!state.selectedId) return;
  const snapshot = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/snapshot`);
  const eventPayload = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/events?after=${state.cursor}`);
  state.snapshot = snapshot;
  const incoming = eventPayload.events || [];
  if (incoming.length) {
    state.timeline.push(...incoming);
    enqueueLiveScreens(incoming);
  }
  state.cursor = eventPayload.cursor || state.cursor;
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
  el.new_dialog.showModal();
  el.initial_message.focus();
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
  el.close_dialog.addEventListener("click", () => el.new_dialog.close());
  el.cancel_new.addEventListener("click", () => el.new_dialog.close());
  el.new_form.addEventListener("submit", async (event) => {
    event.preventDefault();
    el.start_button.disabled = true;
    el.new_error.hidden = true;
    try {
      const typingProfile = document.querySelector('input[name="typing-profile"]:checked')?.value || "balanced";
      const response = await api("/api/meetings", { method: "POST", body: JSON.stringify({ message: el.initial_message.value, model: el.model_input.value.trim() || null, typing_profile: typingProfile }) });
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
      const payload = { message, delivery: el.delivery_mode.value };
      if (state.reference) payload.reference = state.reference;
      await postMeetingAction("messages", payload);
      el.message_input.value = "";
      setReference(null);
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
  el.chat_list.addEventListener("mouseup", () => {
    const selection = window.getSelection();
    const text = selection?.toString() || "";
    const node = selection?.anchorNode?.parentElement?.closest?.("article[data-event-id]");
    if (!text || !node) return;
    const event = state.timeline.find((item) => item.event_id === node.dataset.eventId);
    if (!event || event.kind !== "chat") return;
    const start = event.content.indexOf(text);
    if (start < 0) return;
    setReference({ kind: "chat", event_id: event.event_id, region: { start, end: start + text.length }, excerpt: text });
    selection.removeAllRanges();
  });
  el.chat_list.addEventListener("scroll", () => {
    if (state.renderingChat) return;
    const remaining = el.chat_list.scrollHeight - el.chat_list.clientHeight - el.chat_list.scrollTop;
    state.chatFollow = remaining <= 12;
  }, { passive: true });
  el.screen_reference_button.addEventListener("click", () => {
    state.selectingScreen = !state.selectingScreen;
    el.screen_reference_button.classList.toggle("active", state.selectingScreen);
    el.screen_reference_button.textContent = state.selectingScreen ? "拖拽选择中" : "圈选 Screen";
  });
  el.terminal_screen.addEventListener("mouseup", () => setTimeout(captureScreenTextSelection, 0));
  el.free_scroll_button.addEventListener("click", () => setViewportMode("free"));
  el.follow_scroll_button.addEventListener("click", () => setViewportMode("follow"));
  el.playback_slider.addEventListener("input", () => { state.followLive = false; state.playbackIndex = Number(el.playback_slider.value); stopLiveScreenQueue(); stopPlayback(); renderAll(); });
  el.live_button.addEventListener("click", () => { state.followLive = true; state.playbackIndex = state.timeline.length - 1; stopLiveScreenQueue(); stopPlayback(); renderAll(); });
  el.play_button.addEventListener("click", () => {
    if (state.playing) { stopPlayback(); return; }
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
    await Promise.all([loadHealth(), loadMeetings()]);
    if (state.meetings.length) await selectMeeting(state.meetings[0].meeting_id);
    else renderAll();
  } catch (error) { toast(error.message); }
  setInterval(() => { if (state.snapshot) renderHeader(); }, 1000);
}

boot();
