"use strict";

const elements = {
  healthDot: document.querySelector("#health-dot"),
  newButton: document.querySelector("#new-meeting-button"),
  newPanel: document.querySelector("#new-meeting-panel"),
  closeNew: document.querySelector("#close-new-meeting"),
  newForm: document.querySelector("#new-meeting-form"),
  briefInput: document.querySelector("#brief-input"),
  modelInput: document.querySelector("#model-input"),
  timeoutInput: document.querySelector("#timeout-input"),
  intervalInput: document.querySelector("#interval-input"),
  startButton: document.querySelector("#start-meeting-button"),
  meetingCount: document.querySelector("#meeting-count"),
  meetingList: document.querySelector("#meeting-list"),
  emptyState: document.querySelector("#empty-state"),
  meetingView: document.querySelector("#meeting-view"),
  meetingTitle: document.querySelector("#meeting-title"),
  meetingStatus: document.querySelector("#meeting-status"),
  currentTurn: document.querySelector("#current-turn-label"),
  runtime: document.querySelector("#runtime-label"),
  session: document.querySelector("#session-label"),
  refreshButton: document.querySelector("#refresh-button"),
  stopButton: document.querySelector("#stop-button"),
  turnRail: document.querySelector("#turn-rail"),
  turnProgress: document.querySelector("#turn-progress"),
  chatCount: document.querySelector("#chat-count"),
  conversation: document.querySelector("#conversation-list"),
  screenCount: document.querySelector("#screen-count"),
  proposerTerminal: document.querySelector("#proposer-terminal"),
  reviewerTerminal: document.querySelector("#reviewer-terminal"),
  proposerScreenTime: document.querySelector("#proposer-screen-time"),
  reviewerScreenTime: document.querySelector("#reviewer-screen-time"),
  playButton: document.querySelector("#play-button"),
  liveButton: document.querySelector("#live-button"),
  slider: document.querySelector("#playback-slider"),
  playbackPosition: document.querySelector("#playback-position"),
  playbackTotal: document.querySelector("#playback-total"),
  liveIndicator: document.querySelector("#live-indicator"),
  eventDescription: document.querySelector("#event-description"),
  eventTime: document.querySelector("#event-time"),
  drawer: document.querySelector("#evidence-drawer"),
  drawerTitle: document.querySelector("#drawer-title"),
  drawerContent: document.querySelector("#drawer-content"),
  closeDrawer: document.querySelector("#close-drawer"),
  toast: document.querySelector("#toast"),
};

const state = {
  csrfToken: "",
  csrfHeader: "X-RemoteBuddy-CSRF",
  meetings: [],
  selectedId: null,
  snapshot: null,
  timeline: [],
  eventKeys: new Set(),
  cursors: { chat_after: -1, proposer_after: -1, reviewer_after: -1 },
  playbackIndex: -1,
  followLive: true,
  playing: false,
  playTimer: null,
  pollTimer: null,
  selectionEpoch: 0,
  toastTimer: null,
};

const statusLabels = {
  created: "已创建",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  invalid: "记录损坏",
};

const roleLabels = { proposer: "提案者", reviewer: "审阅者" };
const turnLabels = ["形成提案", "提出质疑", "修订方案"];
const terminalStatuses = new Set(["completed", "failed", "interrupted", "invalid"]);

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function formatDate(value, includeDate = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const options = includeDate
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function formatDuration(startValue, endValue) {
  const start = new Date(startValue).getTime();
  const end = endValue ? new Date(endValue).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "00:00";
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function eventKey(event) {
  return `${event.kind}:${event.event_id}`;
}

function timelineCompare(a, b) {
  const timeOrder = String(a.captured_at || "").localeCompare(String(b.captured_at || ""));
  if (timeOrder) return timeOrder;
  if (a.kind !== b.kind) return a.kind === "screen" ? -1 : 1;
  return Number(a.seq || 0) - Number(b.seq || 0);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if ((options.method || "GET") !== "GET") headers.set(state.csrfHeader, state.csrfToken);
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  let payload = null;
  if (response.status !== 204 && response.status !== 304) {
    const text = await response.text();
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_error) {
      payload = { error: text || "服务返回了无法识别的响应" };
    }
  }
  if (!response.ok && response.status !== 304) {
    throw new Error(payload?.error || `请求失败 (${response.status})`);
  }
  return payload;
}

function showToast(message) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 5000);
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    elements.healthDot.classList.toggle("ok", Boolean(health.ok));
    elements.healthDot.title = health.ok ? "运行环境就绪" : "运行环境有缺失项";
  } catch (error) {
    elements.healthDot.classList.remove("ok");
    elements.healthDot.title = "服务不可用";
  }
}

async function loadMeetings({ preserveSelection = true } = {}) {
  const payload = await api("/api/meetings");
  state.meetings = payload.meetings || [];
  renderMeetingList();
  if (!preserveSelection && !state.selectedId) {
    const fromHash = new URLSearchParams(location.hash.replace(/^#/, "")).get("meeting");
    const initial = state.meetings.find((meeting) => meeting.meeting_id === fromHash && meeting.status !== "invalid")
      || state.meetings.find((meeting) => meeting.status !== "invalid");
    if (initial) await selectMeeting(initial.meeting_id);
  }
}

function renderMeetingList() {
  elements.meetingCount.textContent = String(state.meetings.length);
  elements.meetingList.replaceChildren();
  if (!state.meetings.length) {
    elements.meetingList.append(create("div", "meeting-list-empty", "还没有会议记录。启动第一场会议后，实时进程会显示在这里。"));
    return;
  }
  for (const meeting of state.meetings) {
    const button = create("button", "meeting-list-item");
    button.type = "button";
    button.classList.toggle("active", meeting.meeting_id === state.selectedId);
    button.disabled = meeting.status === "invalid";
    button.dataset.meetingId = meeting.meeting_id;

    const top = create("div", "list-item-top");
    const id = create("span", "list-item-id", meeting.meeting_id);
    const dot = create("span", `status-dot ${meeting.status || "invalid"}`);
    dot.title = statusLabels[meeting.status] || meeting.status;
    top.append(id, dot);

    const bottom = create("div", "list-item-bottom");
    bottom.append(
      create("span", "", `${meeting.completed_turns || 0} / ${meeting.total_turns || 3} 轮`),
      create("time", "", formatDate(meeting.updated_at, true)),
    );
    button.append(top, bottom);
    if (meeting.failure) button.append(create("div", "list-failure", meeting.failure));
    button.addEventListener("click", () => selectMeeting(meeting.meeting_id));
    elements.meetingList.append(button);
  }
}

function clearPolling() {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function stopPlayback() {
  state.playing = false;
  window.clearInterval(state.playTimer);
  state.playTimer = null;
  elements.playButton.textContent = "▶";
  elements.playButton.setAttribute("aria-label", "播放");
  elements.playButton.title = "播放";
}

async function selectMeeting(meetingId) {
  if (!meetingId || meetingId === state.selectedId && state.snapshot) return;
  clearPolling();
  stopPlayback();
  closeEvidenceDrawer();
  state.selectedId = meetingId;
  state.snapshot = null;
  state.timeline = [];
  state.eventKeys = new Set();
  state.cursors = { chat_after: -1, proposer_after: -1, reviewer_after: -1 };
  state.playbackIndex = -1;
  state.followLive = true;
  const epoch = ++state.selectionEpoch;
  renderMeetingList();
  elements.emptyState.hidden = true;
  elements.meetingView.hidden = false;
  elements.meetingTitle.textContent = meetingId;
  elements.meetingStatus.textContent = "载入中";
  elements.meetingStatus.className = "status-badge";
  history.replaceState(null, "", `#meeting=${encodeURIComponent(meetingId)}`);
  try {
    const [snapshot, events] = await Promise.all([
      api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`),
      api(`/api/meetings/${encodeURIComponent(meetingId)}/events?chat_after=-1&proposer_after=-1&reviewer_after=-1`),
    ]);
    if (epoch !== state.selectionEpoch) return;
    state.snapshot = snapshot;
    state.cursors = events.cursors;
    mergeEvents(events.timeline || []);
    state.playbackIndex = state.timeline.length - 1;
    renderAll();
    schedulePoll();
  } catch (error) {
    if (epoch === state.selectionEpoch) showToast(error.message);
  }
}

function mergeEvents(events) {
  let added = 0;
  for (const event of events) {
    const key = eventKey(event);
    if (state.eventKeys.has(key)) continue;
    state.eventKeys.add(key);
    state.timeline.push(event);
    added += 1;
  }
  if (added) state.timeline.sort(timelineCompare);
  return added;
}

function schedulePoll() {
  clearPolling();
  if (!state.snapshot || terminalStatuses.has(state.snapshot.meeting.status)) return;
  state.pollTimer = window.setTimeout(pollSelectedMeeting, 1000);
}

async function pollSelectedMeeting() {
  if (!state.selectedId) return;
  const meetingId = state.selectedId;
  const epoch = state.selectionEpoch;
  const query = new URLSearchParams(state.cursors).toString();
  try {
    const [snapshot, events] = await Promise.all([
      api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`),
      api(`/api/meetings/${encodeURIComponent(meetingId)}/events?${query}`),
    ]);
    if (epoch !== state.selectionEpoch || meetingId !== state.selectedId) return;
    const wasLive = state.followLive;
    const added = mergeEvents(events.timeline || []);
    state.snapshot = snapshot;
    state.cursors = events.cursors;
    if (wasLive) state.playbackIndex = state.timeline.length - 1;
    renderAll();
    if (added || terminalStatuses.has(snapshot.meeting.status)) {
      loadMeetings().catch(() => {});
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    if (epoch === state.selectionEpoch) schedulePoll();
  }
}

function activeTurnLabel(meeting) {
  if (meeting.status === "completed") return "三轮已完成";
  if (meeting.status === "failed") return "执行失败";
  if (meeting.status === "interrupted") return "会议已停止";
  if (meeting.current_turn) return meeting.current_turn;
  const next = (meeting.turns || []).find((turn) => turn.status !== "completed");
  if (next && meeting.status === "running") return next.turn_id;
  return "等待启动";
}

function renderHeader() {
  const meeting = state.snapshot.meeting;
  elements.meetingTitle.textContent = meeting.meeting_id;
  elements.meetingStatus.textContent = statusLabels[meeting.status] || meeting.status;
  elements.meetingStatus.className = `status-badge ${meeting.status}`;
  elements.currentTurn.textContent = activeTurnLabel(meeting);
  const endValue = terminalStatuses.has(meeting.status) ? meeting.updated_at : null;
  elements.runtime.textContent = formatDuration(meeting.created_at, endValue);
  elements.session.textContent = `session ${meeting.session_state || "unknown"}`;
  const canStop = ["created", "running"].includes(meeting.status)
    || ["running", "retained"].includes(meeting.session_state);
  elements.stopButton.disabled = !canStop || meeting.session_state === "stopped";
  document.title = `${meeting.meeting_id} · RemoteBuddy`;
}

function visibleEvents() {
  return state.playbackIndex >= 0 ? state.timeline.slice(0, state.playbackIndex + 1) : [];
}

function renderTurns(events) {
  const turns = state.snapshot.meeting.turns || [];
  const visibleChats = new Set(events.filter((event) => event.kind === "chat").map((event) => event.turn_id));
  const latestEvent = events[events.length - 1];
  const isLive = state.followLive;
  elements.turnRail.replaceChildren();
  let completeCount = 0;
  turns.forEach((turn, index) => {
    let viewStatus = "pending";
    if (visibleChats.has(turn.turn_id)) viewStatus = "completed";
    else if (latestEvent?.turn_id === turn.turn_id) viewStatus = "running";
    else if (isLive) viewStatus = turn.status;
    if (viewStatus === "completed") completeCount += 1;

    const button = create("button", `turn-node ${viewStatus === "completed" ? "complete" : viewStatus}`);
    button.type = "button";
    button.title = `查看 ${turn.turn_id} 的证据`;
    const head = create("div", "turn-node-head");
    const number = create("span", "turn-index", viewStatus === "completed" ? "✓" : String(index + 1).padStart(2, "0"));
    const speaker = create("span", "turn-speaker", `${roleLabels[turn.speaker] || turn.speaker} · ${turnLabels[index] || "会议回合"}`);
    head.append(number, speaker);
    const objective = create("span", "turn-objective", turn.objective);
    const time = create("time", "turn-time");
    if (viewStatus === "completed") time.textContent = `完成 ${formatDate(turn.completed_at)}`;
    else if (viewStatus === "running") time.textContent = `开始 ${formatDate(turn.started_at)}`;
    else time.textContent = "等待前序回合";
    button.append(head, objective, time);
    button.addEventListener("click", () => openTurnEvidence(turn.turn_id));
    elements.turnRail.append(button);
  });
  elements.turnProgress.textContent = `${completeCount} / ${turns.length}`;
}

function renderConversation(events) {
  const chats = events.filter((event) => event.kind === "chat");
  elements.chatCount.textContent = `${chats.length} 条`;
  elements.conversation.replaceChildren();
  if (!chats.length) {
    elements.conversation.append(create("div", "conversation-empty", "等待 Agent 提交第一轮提案…"));
    return;
  }
  for (const event of chats) {
    const article = create("article", `chat-message ${event.participant}`);
    const header = create("header");
    const speaker = create("span", `speaker-label ${event.participant}`, `${roleLabels[event.participant] || event.participant} · ${event.turn_id}`);
    const time = create("time", "", formatDate(event.captured_at));
    header.append(speaker, time);
    const body = create("pre", "", event.content || "");
    article.append(header, body);
    elements.conversation.append(article);
  }
  if (state.followLive) elements.conversation.scrollTop = elements.conversation.scrollHeight;
}

function latestScreen(events, participant) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind === "screen" && event.participant === participant) return event;
  }
  return null;
}

function setTerminal(participant, event) {
  const terminal = participant === "proposer" ? elements.proposerTerminal : elements.reviewerTerminal;
  const time = participant === "proposer" ? elements.proposerScreenTime : elements.reviewerScreenTime;
  terminal.textContent = event?.content || `等待 ${participant} 终端画面…`;
  time.textContent = event ? formatDate(event.captured_at) : "暂无画面";
  if (event) {
    terminal.dataset.eventId = event.event_id;
    requestAnimationFrame(() => {
      terminal.scrollTop = terminal.scrollHeight;
      terminal.scrollLeft = 0;
    });
  } else {
    delete terminal.dataset.eventId;
  }
}

function renderTerminals(events) {
  const screens = events.filter((event) => event.kind === "screen");
  elements.screenCount.textContent = `${screens.length} 帧`;
  setTerminal("proposer", latestScreen(events, "proposer"));
  setTerminal("reviewer", latestScreen(events, "reviewer"));
}

function describeEvent(event) {
  if (!event) return "等待事件";
  const role = roleLabels[event.participant] || event.participant || "Harness";
  if (event.kind === "chat") return `${role} 的 ${event.turn_id} 发言已被接受`;
  const phase = event.phase || "终端采集";
  return `${role} 终端画面 · ${phase}`;
}

function renderPlayback() {
  const total = state.timeline.length;
  const max = total - 1;
  state.playbackIndex = Math.min(Math.max(state.playbackIndex, -1), max);
  elements.slider.min = "-1";
  elements.slider.max = String(max);
  elements.slider.value = String(state.playbackIndex);
  elements.slider.disabled = total === 0;
  elements.playbackPosition.textContent = String(state.playbackIndex + 1);
  elements.playbackTotal.textContent = String(total);
  const current = state.timeline[state.playbackIndex];
  elements.eventDescription.textContent = describeEvent(current);
  elements.eventTime.textContent = current ? formatDate(current.captured_at) : "";
  elements.liveIndicator.classList.toggle("paused", !state.followLive);
  elements.liveIndicator.lastChild.textContent = state.followLive ? " 实时" : " 已暂停";
  elements.liveButton.disabled = state.followLive;
  elements.playButton.disabled = total === 0;
}

function renderAll() {
  if (!state.snapshot) return;
  renderHeader();
  const events = visibleEvents();
  renderTurns(events);
  renderConversation(events);
  renderTerminals(events);
  renderPlayback();
}

function togglePlayback() {
  if (!state.timeline.length) return;
  if (state.playing) {
    stopPlayback();
    return;
  }
  if (state.playbackIndex >= state.timeline.length - 1) state.playbackIndex = -1;
  state.followLive = false;
  state.playing = true;
  elements.playButton.textContent = "Ⅱ";
  elements.playButton.setAttribute("aria-label", "暂停");
  elements.playButton.title = "暂停";
  renderAll();
  state.playTimer = window.setInterval(() => {
    if (state.playbackIndex < state.timeline.length - 1) {
      state.playbackIndex += 1;
      renderAll();
      return;
    }
    stopPlayback();
  }, 250);
}

function goLive() {
  stopPlayback();
  state.followLive = true;
  state.playbackIndex = state.timeline.length - 1;
  renderAll();
}

function summaryField(term, value, className = "") {
  const wrapper = create("div");
  const dt = create("dt", "", term);
  const dd = create("dd", className, value || "—");
  wrapper.append(dt, dd);
  return wrapper;
}

async function openTurnEvidence(turnId) {
  if (!state.selectedId) return;
  elements.drawerTitle.textContent = turnId;
  elements.drawerContent.replaceChildren(create("div", "conversation-empty", "正在读取回合证据…"));
  elements.drawer.classList.add("open");
  elements.drawer.setAttribute("aria-hidden", "false");
  try {
    const detail = await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/turns/${encodeURIComponent(turnId)}`);
    if (detail.meeting_id !== state.selectedId) return;
    renderEvidence(detail);
  } catch (error) {
    elements.drawerContent.replaceChildren(create("div", "list-failure", error.message));
  }
}

function evidenceBlock(title, content) {
  const section = create("section", "evidence-block");
  section.append(create("h3", "", title), create("pre", "", content || "暂无内容"));
  return section;
}

function renderEvidence(detail) {
  const fragment = document.createDocumentFragment();
  const summary = create("dl", "evidence-summary");
  summary.append(
    summaryField("Speaker", roleLabels[detail.turn.speaker] || detail.turn.speaker),
    summaryField("Status", statusLabels[detail.turn.status] || detail.turn.status),
    summaryField("Completion marker", detail.marker.valid ? "有效" : "无效", detail.marker.valid ? "valid-value" : "invalid-value"),
    summaryField("Chat event", detail.chat_event?.event_id || "尚未接受"),
    summaryField("Source SHA-256", detail.source_sha256),
    summaryField("Manifest SHA-256", detail.manifest_sha256),
  );
  fragment.append(summary);
  fragment.append(evidenceBlock("回合文件原文", detail.raw));
  if (detail.chat_event) fragment.append(evidenceBlock("接受后的聊天事件", detail.chat_event.content));

  const screens = create("section", "evidence-block");
  screens.append(create("h3", "", `相关终端事件 · ${detail.screen_events.length} 帧`));
  const list = create("div", "evidence-event-list");
  const selected = detail.screen_events.length > 24
    ? [...detail.screen_events.slice(0, 12), ...detail.screen_events.slice(-12)]
    : detail.screen_events;
  for (const event of selected) {
    list.append(create("div", "evidence-event-row", `${formatDate(event.captured_at)} · ${event.participant} · ${event.phase} · ${event.event_id}`));
  }
  if (!selected.length) list.append(create("div", "evidence-event-row", "暂无对应终端事件"));
  screens.append(list);
  fragment.append(screens);

  for (const participant of ["proposer", "reviewer"]) {
    const before = detail.screen_before?.[participant];
    const after = detail.screen_after?.[participant];
    if (before || after) {
      const content = [
        before ? `--- ${participant} · 回合前 · ${before.event_id} ---\n${before.content}` : "",
        after ? `--- ${participant} · 回合后 · ${after.event_id} ---\n${after.content}` : "",
      ].filter(Boolean).join("\n\n");
      fragment.append(evidenceBlock(`${roleLabels[participant]}前后快照`, content));
    }
  }
  elements.drawerContent.replaceChildren(fragment);
}

function closeEvidenceDrawer() {
  elements.drawer.classList.remove("open");
  elements.drawer.setAttribute("aria-hidden", "true");
}

async function startMeeting(event) {
  event.preventDefault();
  elements.startButton.disabled = true;
  elements.startButton.textContent = "正在创建…";
  try {
    const payload = {
      brief: elements.briefInput.value,
      model: elements.modelInput.value.trim() || null,
      turn_timeout_seconds: Number(elements.timeoutInput.value),
      capture_interval_seconds: Number(elements.intervalInput.value),
    };
    const response = await api("/api/meetings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    elements.newPanel.hidden = true;
    elements.newButton.hidden = false;
    elements.briefInput.value = "";
    await loadMeetings();
    await selectMeeting(response.meeting_id);
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.startButton.disabled = false;
    elements.startButton.textContent = "启动双 Agent 会议";
  }
}

async function stopMeeting() {
  if (!state.selectedId || !state.snapshot) return;
  const accepted = window.confirm(`停止会议 ${state.selectedId}？终端 session 会被清理，但历史记录会保留。`);
  if (!accepted) return;
  elements.stopButton.disabled = true;
  try {
    await api(`/api/meetings/${encodeURIComponent(state.selectedId)}/stop`, {
      method: "POST",
      body: "{}",
    });
    await refreshSelected();
    await loadMeetings();
  } catch (error) {
    showToast(error.message);
    elements.stopButton.disabled = false;
  }
}

async function refreshSelected() {
  if (!state.selectedId) return;
  const meetingId = state.selectedId;
  const snapshot = await api(`/api/meetings/${encodeURIComponent(meetingId)}/snapshot`);
  const events = await api(`/api/meetings/${encodeURIComponent(meetingId)}/events?chat_after=-1&proposer_after=-1&reviewer_after=-1`);
  if (meetingId !== state.selectedId) return;
  state.snapshot = snapshot;
  state.timeline = [];
  state.eventKeys = new Set();
  mergeEvents(events.timeline || []);
  state.cursors = events.cursors;
  if (state.followLive) state.playbackIndex = state.timeline.length - 1;
  renderAll();
  schedulePoll();
}

function bindEvents() {
  elements.newButton.addEventListener("click", () => {
    elements.newPanel.hidden = false;
    elements.newButton.hidden = true;
    elements.briefInput.focus();
  });
  elements.closeNew.addEventListener("click", () => {
    elements.newPanel.hidden = true;
    elements.newButton.hidden = false;
  });
  elements.newForm.addEventListener("submit", startMeeting);
  elements.refreshButton.addEventListener("click", () => refreshSelected().catch((error) => showToast(error.message)));
  elements.stopButton.addEventListener("click", stopMeeting);
  elements.playButton.addEventListener("click", togglePlayback);
  elements.liveButton.addEventListener("click", goLive);
  elements.slider.addEventListener("input", () => {
    stopPlayback();
    state.playbackIndex = Number(elements.slider.value);
    state.followLive = state.playbackIndex === state.timeline.length - 1;
    renderAll();
  });
  elements.closeDrawer.addEventListener("click", closeEvidenceDrawer);
  elements.drawer.querySelector("[data-close-drawer]").addEventListener("click", closeEvidenceDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeEvidenceDrawer();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.selectedId) refreshSelected().catch(() => {});
  });
}

async function boot() {
  bindEvents();
  try {
    const session = await api("/api/session");
    state.csrfToken = session.csrf_token;
    state.csrfHeader = session.csrf_header || state.csrfHeader;
    await Promise.all([loadHealth(), loadMeetings({ preserveSelection: false })]);
  } catch (error) {
    showToast(error.message);
  }
  window.setInterval(() => {
    if (state.snapshot) renderHeader();
  }, 1000);
}

boot();
