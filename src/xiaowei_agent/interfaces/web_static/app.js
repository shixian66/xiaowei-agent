const TERMINAL_STATUSES = new Set([
  "succeeded",
  "failed",
  "rejected",
  "clarification_required",
  "canceled",
  "indeterminate",
]);

const STATUS = Object.freeze({
  created: { label: "已受理", icon: "●" },
  planning: { label: "正在规划", icon: "●" },
  running: { label: "正在处理", icon: "●" },
  awaiting_approval: { label: "等待审批", icon: "●" },
  succeeded: { label: "处理完成", icon: "✓" },
  failed: { label: "处理失败", icon: "×" },
  rejected: { label: "已拒绝", icon: "!" },
  clarification_required: { label: "需要补充信息", icon: "!" },
  canceled: { label: "已取消", icon: "—" },
  indeterminate: { label: "结果待确认", icon: "?" },
});

const elements = Object.freeze({
  actor: document.querySelector("#actor-name"),
  avatar: document.querySelector("#user-avatar"),
  role: document.querySelector("#role-name"),
  environment: document.querySelector("#environment-name"),
  logout: document.querySelector("#logout-button"),
  refreshList: document.querySelector("#refresh-list"),
  listTitle: document.querySelector("#task-list-title"),
  list: document.querySelector("#task-list"),
  listLoading: document.querySelector("#list-loading"),
  listEmpty: document.querySelector("#list-empty"),
  listError: document.querySelector("#list-error"),
  loadMore: document.querySelector("#load-more"),
  input: document.querySelector("#task-input"),
  submit: document.querySelector("#submit-task"),
  submitMessage: document.querySelector("#submit-message"),
  contextSource: document.querySelector("#context-source"),
  contextTaskId: document.querySelector("#context-task-id"),
  clearContext: document.querySelector("#clear-context"),
  taskEmpty: document.querySelector("#task-empty"),
  taskDetail: document.querySelector("#task-detail"),
  status: document.querySelector("#task-status"),
  request: document.querySelector("#task-request"),
  taskId: document.querySelector("#task-id"),
  parentRow: document.querySelector("#task-parent-row"),
  parent: document.querySelector("#task-parent"),
  submittedAt: document.querySelector("#task-submitted-at"),
  version: document.querySelector("#task-version"),
  timelineStatus: document.querySelector("#timeline-status"),
  pollingState: document.querySelector("#polling-state"),
  progress: document.querySelector("#progress-card"),
  result: document.querySelector("#result-card"),
  answer: document.querySelector("#result-answer"),
  disclosureBlock: document.querySelector("#disclosure-block"),
  disclosure: document.querySelector("#result-disclosure"),
  sections: document.querySelector("#result-sections"),
  nextBlock: document.querySelector("#next-steps-block"),
  nextSteps: document.querySelector("#result-next-steps"),
  refsBlock: document.querySelector("#refs-block"),
  refs: document.querySelector("#result-refs"),
  readError: document.querySelector("#read-error"),
  retryDetail: document.querySelector("#retry-detail"),
  openDetail: document.querySelector("#open-detail"),
  continueTask: document.querySelector("#continue-task"),
});

let csrfToken = null;
let tasks = [];
let nextCursor = null;
let selectedTaskId = null;
let activeFilter = "all";
let pollTimer = null;
let pollDelay = 2000;
let pendingSubmission = null;
let pendingParentTaskId = null;
let requestedParentTaskId = null;

function text(value, fallback = "—") {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function statusInfo(status) {
  return STATUS[status] || { label: "未知状态", icon: "?" };
}

function formatTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function setVisible(element, visible) {
  element.classList.toggle("is-hidden", !visible);
}

function setStatusBadge(element, status) {
  const presentation = statusInfo(status);
  element.replaceChildren();
  const icon = document.createElement("span");
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = presentation.icon;
  const label = document.createElement("span");
  label.textContent = presentation.label;
  element.append(icon, label);
  element.dataset.status = status;
}

function setParentContext(taskId) {
  pendingParentTaskId = typeof taskId === "string" && taskId.length > 0 ? taskId : null;
  elements.contextTaskId.textContent = pendingParentTaskId || "";
  setVisible(elements.contextSource, pendingParentTaskId !== null);
}

function renderParentReference(task) {
  const parentTaskId = typeof task.clarification_parent_task_id === "string" && task.clarification_parent_task_id.length > 0
    ? task.clarification_parent_task_id
    : null;
  elements.parent.textContent = parentTaskId || "";
  elements.parent.href = parentTaskId === null
    ? "/app"
    : `/app/tasks/${encodeURIComponent(parentTaskId)}`;
  setVisible(elements.parentRow, parentTaskId !== null);
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error("request failed");
    error.status = response.status;
    error.code = payload && payload.error ? payload.error.code : "internal_error";
    throw error;
  }
  return payload;
}

function taskMatchesFilter(task) {
  if (activeFilter === "active") {
    return !TERMINAL_STATUSES.has(task.status);
  }
  if (activeFilter === "terminal") {
    return TERMINAL_STATUSES.has(task.status);
  }
  return true;
}

function renderTaskList() {
  elements.list.replaceChildren();
  const visibleTasks = tasks.filter(taskMatchesFilter);
  for (const task of visibleTasks) {
    const item = document.createElement("li");
    item.className = "task-list-item";
    const button = document.createElement("button");
    button.className = "task-list-button";
    button.type = "button";
    button.classList.toggle("is-selected", task.task_id === selectedTaskId);

    const top = document.createElement("span");
    top.className = "task-list-top";
    const badge = document.createElement("span");
    badge.className = "status-badge";
    setStatusBadge(badge, task.status);
    const time = document.createElement("span");
    time.className = "task-list-time";
    time.textContent = formatTime(task.submitted_at);
    top.append(badge, time);

    const request = document.createElement("span");
    request.className = "task-list-request";
    request.textContent = text(task.request_preview, "未提供任务摘要");
    button.append(top, request);
    button.addEventListener("click", () => selectTask(task.task_id));
    item.append(button);
    elements.list.append(item);
  }
  setVisible(elements.listEmpty, visibleTasks.length === 0 && tasks.length === 0);
  setVisible(elements.loadMore, nextCursor !== null);
}

async function fetchVisibleTaskPage(cursor) {
  let current = cursor;
  const seen = new Set();
  while (true) {
    const query = new URLSearchParams({ limit: "20" });
    if (current !== null) {
      query.set("before_created_seq", String(current));
    }
    const page = await requestJson(`/app/api/tasks?${query.toString()}`);
    if (!page || !Array.isArray(page.items)) {
      throw new Error("invalid task page");
    }
    if (page.items.length > 0 || page.next_created_seq === null) {
      return page;
    }
    const following = page.next_created_seq;
    if (!Number.isInteger(following) || following <= 0 || seen.has(following)) {
      throw new Error("invalid task cursor");
    }
    if (current !== null && following >= current) {
      throw new Error("task cursor did not advance");
    }
    seen.add(following);
    current = following;
  }
}

async function loadTasks({ append = false } = {}) {
  setVisible(elements.listLoading, true);
  setVisible(elements.listError, false);
  try {
    const page = await fetchVisibleTaskPage(append ? nextCursor : null);
    tasks = append ? [...tasks, ...page.items] : page.items;
    nextCursor = page.next_created_seq;
    renderTaskList();
  } catch (error) {
    setVisible(elements.listError, true);
    if (error.status === 401) {
      window.location.assign("/app");
    }
  } finally {
    setVisible(elements.listLoading, false);
  }
}

function clearResult() {
  elements.answer.textContent = "";
  elements.disclosure.replaceChildren();
  elements.sections.replaceChildren();
  elements.nextSteps.replaceChildren();
  elements.refs.replaceChildren();
  setVisible(elements.nextBlock, false);
  setVisible(elements.refsBlock, false);
  setVisible(elements.disclosureBlock, false);
  setVisible(elements.result, false);
}

function renderDisclosure(disclosure) {
  if (!disclosure || typeof disclosure !== "object") {
    return;
  }
  const resourceIds = Array.isArray(disclosure.resource_ids) ? disclosure.resource_ids.join(",") : "—";
  const rows = [
    ["能力", `${text(disclosure.capability_id)}@${text(disclosure.capability_version)}`],
    ["环境", text(disclosure.environment_id)],
    ["目标", `${text(disclosure.provider)}/${text(disclosure.resource_kind)}/${resourceIds}`],
    ["计划分类", text(disclosure.plan_disposition)],
    ["外部目标访问", disclosure.external_target_access === true ? "true" : "false"],
  ];
  for (const [label, value] of rows) {
    const item = document.createElement("li");
    item.textContent = `${label}：${value}`;
    elements.disclosure.append(item);
  }
  setVisible(elements.disclosureBlock, true);
}

function appendReferences(parent, references) {
  if (!Array.isArray(references) || references.length === 0) {
    return;
  }
  const list = document.createElement("ul");
  list.className = "ref-list";
  for (const reference of references) {
    const item = document.createElement("li");
    item.textContent = text(reference, "未知引用");
    list.append(item);
  }
  parent.append(list);
}

function renderSafeResult(render) {
  clearResult();
  if (!render || typeof render !== "object") {
    return;
  }
  elements.answer.textContent = text(render.answer, "结果未提供结论");
  if (Array.isArray(render.sections)) {
    for (const section of render.sections) {
      const block = document.createElement("section");
      block.className = "result-section";
      const title = document.createElement("h4");
      title.textContent = text(section.title, "证据摘要");
      const body = document.createElement("p");
      body.textContent = text(section.body, "未提供摘要");
      block.append(title, body);
      appendReferences(block, section.refs);
      elements.sections.append(block);
    }
  }
  if (Array.isArray(render.next_steps) && render.next_steps.length > 0) {
    for (const step of render.next_steps) {
      const item = document.createElement("li");
      item.textContent = text(step, "未提供建议");
      elements.nextSteps.append(item);
    }
    setVisible(elements.nextBlock, true);
  }
  if (Array.isArray(render.refs) && render.refs.length > 0) {
    for (const reference of render.refs) {
      const item = document.createElement("li");
      item.textContent = text(reference, "未知引用");
      elements.refs.append(item);
    }
    setVisible(elements.refsBlock, true);
  }
  setVisible(elements.result, true);
}

function renderTaskDetail(task) {
  setVisible(elements.taskEmpty, false);
  setVisible(elements.taskDetail, true);
  setVisible(elements.readError, false);
  setStatusBadge(elements.status, task.status);
  elements.request.textContent = text(task.request_preview, "未提供任务摘要");
  elements.taskId.textContent = text(task.task_id);
  renderParentReference(task);
  elements.submittedAt.textContent = formatTime(task.submitted_at);
  elements.version.textContent = Number.isInteger(task.task_version) ? String(task.task_version) : "—";
  elements.timelineStatus.textContent = statusInfo(task.status).label;
  elements.openDetail.href = text(task.detail_path, "/app");
  setVisible(elements.openDetail, true);
  const terminal = TERMINAL_STATUSES.has(task.status);
  const canContinue = task.status === "clarification_required";
  setVisible(elements.continueTask, canContinue);
  setVisible(elements.progress, !terminal);
  if (terminal) {
    elements.pollingState.textContent = "已停止轮询";
    renderSafeResult(task.render);
    renderDisclosure(task.disclosure);
    if (canContinue && requestedParentTaskId === task.task_id) {
      setParentContext(task.task_id);
      requestedParentTaskId = null;
      elements.input.focus();
    }
  } else {
    elements.pollingState.textContent = document.visibilityState === "hidden" ? "后台低频同步" : "自动同步";
    clearResult();
  }
  tasks = tasks.map(item => item.task_id === task.task_id ? { ...item, status: task.status } : item);
  renderTaskList();
  return terminal;
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  if (selectedTaskId === null) {
    return;
  }
  const delay = document.visibilityState === "hidden" ? Math.max(30000, pollDelay) : pollDelay;
  pollTimer = window.setTimeout(() => readSelectedTask(), delay);
  pollDelay = Math.min(10000, Math.max(2000, Math.round(pollDelay * 1.6)));
}

async function readSelectedTask({ immediate = false } = {}) {
  if (selectedTaskId === null) {
    return;
  }
  window.clearTimeout(pollTimer);
  setVisible(elements.readError, false);
  if (immediate) {
    elements.pollingState.textContent = "正在同步";
  }
  try {
    const task = await requestJson(`/app/api/tasks/${encodeURIComponent(selectedTaskId)}`);
    const terminal = renderTaskDetail(task);
    if (terminal) {
      return;
    }
  } catch (error) {
    setVisible(elements.progress, false);
    setVisible(elements.readError, true);
    elements.pollingState.textContent = "读取暂时中断";
    if (error.status === 401 || error.status === 404) {
      clearResult();
      setVisible(elements.taskDetail, false);
      setVisible(elements.taskEmpty, true);
      if (error.status === 404) {
        tasks = tasks.filter(task => task.task_id !== selectedTaskId);
        selectedTaskId = null;
        renderTaskList();
        return;
      }
      window.location.assign("/app");
      return;
    }
  }
  schedulePoll();
}

function selectTask(taskId) {
  selectedTaskId = taskId;
  pollDelay = 2000;
  renderTaskList();
  readSelectedTask({ immediate: true });
}

function setSubmitState({ busy, ambiguous = false, message = "" }) {
  elements.submit.disabled = busy;
  elements.input.disabled = busy || ambiguous;
  elements.clearContext.disabled = busy || ambiguous;
  elements.continueTask.disabled = busy || ambiguous;
  elements.submit.querySelector("span").textContent = ambiguous ? "重试发送" : busy ? "正在发送" : "发送任务";
  elements.submitMessage.textContent = message;
  setVisible(elements.submitMessage, message.length > 0);
}

async function submitTask() {
  const draft = elements.input.value;
  if (draft.trim().length === 0) {
    setSubmitState({ busy: false, message: "请先说明你想让小维检查什么。" });
    return;
  }
  if (pendingSubmission === null) {
    pendingSubmission = {
      id: crypto.randomUUID(),
      text: draft,
      parentTaskId: pendingParentTaskId,
    };
  }
  setSubmitState({ busy: true });
  try {
    const body = {
      text: pendingSubmission.text,
      client_submission_id: pendingSubmission.id,
    };
    if (pendingSubmission.parentTaskId !== null) {
      body.clarification_parent_task_id = pendingSubmission.parentTaskId;
    }
    const accepted = await requestJson("/app/api/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(body),
    });
    pendingSubmission = null;
    setParentContext(null);
    elements.input.value = "";
    setSubmitState({ busy: false });
    await loadTasks();
    selectTask(accepted.task_id);
  } catch (error) {
    const ambiguous = !Number.isInteger(error.status) || error.status >= 500;
    if (!ambiguous) {
      pendingSubmission = null;
    }
    const message = ambiguous
      ? "发送结果暂时无法确认。请重试，本次会继续使用同一个请求标识。"
      : error.status === 409
        ? "这次请求标识已用于另一段内容，请重新输入后发送。"
        : error.status === 403
          ? "当前账号没有发起只读任务的权限。"
          : "任务内容不符合提交要求，请检查后重试。";
    setSubmitState({ busy: false, ambiguous, message });
    if (error.status === 401) {
      window.location.assign("/app");
    }
  }
}

async function loadIdentity() {
  const me = await requestJson("/app/api/me");
  csrfToken = me.csrf_token;
  elements.actor.textContent = text(me.actor, "授权用户");
  elements.avatar.textContent = text(me.actor, "用").slice(0, 1).toUpperCase();
  elements.environment.textContent = `${text(me.environment_id, "未知")} 环境`;
  const admin = Array.isArray(me.permissions) && me.permissions.includes("admin_all_safe_tasks");
  elements.role.textContent = admin ? "Admin · 全部安全任务" : "授权运维用户";
  elements.listTitle.textContent = admin ? "全部安全任务" : "我的任务";
}

async function logout() {
  elements.logout.disabled = true;
  try {
    await requestJson("/app/api/logout", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: "{}",
    });
  } finally {
    window.location.assign("/app");
  }
}

for (const tab of document.querySelectorAll(".filter-tab")) {
  tab.addEventListener("click", () => {
    activeFilter = tab.dataset.filter;
    for (const candidate of document.querySelectorAll(".filter-tab")) {
      candidate.classList.toggle("is-active", candidate === tab);
    }
    renderTaskList();
  });
}

elements.submit.addEventListener("click", submitTask);
elements.input.addEventListener("keydown", event => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    submitTask();
  }
});
elements.logout.addEventListener("click", logout);
elements.refreshList.addEventListener("click", () => loadTasks());
elements.loadMore.addEventListener("click", () => loadTasks({ append: true }));
elements.retryDetail.addEventListener("click", () => readSelectedTask({ immediate: true }));
elements.continueTask.addEventListener("click", () => {
  const selected = tasks.find(task => task.task_id === selectedTaskId);
  if (selected && selected.status === "clarification_required") {
    setParentContext(selectedTaskId);
    elements.input.focus();
  }
});
elements.clearContext.addEventListener("click", () => setParentContext(null));
document.addEventListener("visibilitychange", () => {
  if (selectedTaskId !== null && document.visibilityState === "visible") {
    pollDelay = 2000;
    readSelectedTask({ immediate: true });
  } else {
    schedulePoll();
  }
});


// --------------------------------------------------------------------------
// 配置面
// --------------------------------------------------------------------------

// 与服务端锁定的常量**必须**一字不差；由 tests/security/test_ri5_compose_boundary.py
// 的 test_the_page_shows_the_same_fixed_gemini_constants_as_the_adapter 反向承重，
// 抄错或改动一侧会直接变红。
const GEMINI_FIXED = Object.freeze({
  model: "gemini-3-flash-preview",
  apiVersion: "v1beta",
  origin: "https://generativelanguage.googleapis.com",
});

const CHECK_NAMES = Object.freeze([
  "gemini_connection",
  "feishu_credentials",
  "feishu_oauth",
]);

const DISPLAY_STATE = Object.freeze({
  unconfigured: "未配置",
  pending_restart: "待应用（需重启进程）",
  pending_test: "待测试",
  available: "可用",
  test_failed: "测试未通过",
});

const PROBE_ERROR = Object.freeze({
  real_test_disabled: "真实测试开关未打开",
  not_configured: "尚未配置",
  unauthorized: "凭据被拒绝",
  timeout: "超时",
  unavailable: "暂时不可用",
  invalid_response: "响应不合法",
});

const configElements = Object.freeze({
  panel: document.querySelector("#config-panel"),
  generation: document.querySelector("#config-generation"),
  restartNotice: document.querySelector("#config-restart-notice"),
  message: document.querySelector("#config-message"),
  geminiModel: document.querySelector("#gemini-model"),
  geminiApiVersion: document.querySelector("#gemini-api-version"),
  geminiOrigin: document.querySelector("#gemini-origin"),
  geminiConfigured: document.querySelector("#gemini-configured"),
  geminiEnabled: document.querySelector("#gemini-enabled"),
  geminiApiKey: document.querySelector("#gemini-api-key"),
  feishuCallback: document.querySelector("#feishu-callback"),
  feishuConfigured: document.querySelector("#feishu-configured"),
  feishuEnabled: document.querySelector("#feishu-enabled"),
  feishuAppId: document.querySelector("#feishu-app-id"),
  feishuAppSecret: document.querySelector("#feishu-app-secret"),
});

function setConfigMessage(message) {
  configElements.message.textContent = message || "";
  setVisible(configElements.message, Boolean(message));
}

function renderChecks(checks) {
  for (const name of CHECK_NAMES) {
    const badge = document.querySelector(`#check-${name}`);
    const state = checks[name];
    // 一律 textContent：这些值来自服务端，绝不走任何 HTML 解析汇聚点。
    badge.textContent = DISPLAY_STATE[state] || "—";
    badge.dataset.state = typeof state === "string" ? state : "";
  }
}

function renderConfig(view) {
  configElements.generation.textContent = `代次 ${view.generation}`;
  configElements.geminiModel.textContent = GEMINI_FIXED.model;
  configElements.geminiApiVersion.textContent = GEMINI_FIXED.apiVersion;
  configElements.geminiOrigin.textContent = GEMINI_FIXED.origin;
  configElements.feishuCallback.textContent = `${window.location.origin}/oauth/feishu/callback`;

  configElements.geminiConfigured.textContent = view.gemini.configured ? "已配置" : "未配置";
  configElements.geminiEnabled.checked = view.gemini.enabled === true;
  configElements.feishuConfigured.textContent = view.feishu.configured ? "已配置" : "未配置";
  configElements.feishuEnabled.checked = view.feishu.enabled === true;
  configElements.feishuAppId.value = text(view.feishu.app_id, "");

  // Secret 输入框在每次渲染后清空：页面从不回显凭据，留着旧输入会让下一次保存
  // 把一个用户以为已经放弃的值再写回去。
  configElements.geminiApiKey.value = "";
  configElements.feishuAppSecret.value = "";

  renderChecks(view.checks);
  for (const name of CHECK_NAMES) {
    const provider = name === "gemini_connection" ? view.gemini : view.feishu;
    document.querySelector(`#test-${name}`).disabled =
      provider.enabled !== true || provider.configured !== true;
  }
}

async function loadConfig() {
  const view = await requestJson("/app/api/config");
  renderConfig(view);
  setVisible(configElements.panel, true);
}

async function saveConfig(body) {
  setConfigMessage("");
  try {
    const saved = await requestJson("/app/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body),
    });
    setVisible(configElements.restartNotice, saved.restart_required === true);
    await loadConfig();
  } catch (error) {
    setConfigMessage(error.code === "unavailable"
      ? "配置文件当前不可读写，已保存的配置未被修改。"
      : "保存被拒绝，配置未改变。");
  }
}

async function clearProvider(provider) {
  setConfigMessage("");
  try {
    await requestJson("/app/api/config/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ provider }),
    });
    setVisible(configElements.restartNotice, true);
    await loadConfig();
  } catch (_) {
    setConfigMessage("清除被拒绝，配置未改变。");
  }
}

function providerUpdate(provider) {
  // 只携带真正填了的字段：未携带 = 保留原值。空串与显式 null 都会被服务端拒绝，
  // 因此"留空表示不修改"必须在这里体现为**不发这个键**。
  if (provider === "gemini") {
    const update = { enabled: configElements.geminiEnabled.checked };
    if (configElements.geminiApiKey.value.length > 0) {
      update.api_key = configElements.geminiApiKey.value;
    }
    return { gemini: update };
  }
  const update = { enabled: configElements.feishuEnabled.checked };
  if (configElements.feishuAppId.value.length > 0) {
    update.app_id = configElements.feishuAppId.value;
  }
  if (configElements.feishuAppSecret.value.length > 0) {
    update.app_secret = configElements.feishuAppSecret.value;
  }
  return { feishu: update };
}

async function runProviderTest(name) {
  setConfigMessage("");
  let result = null;
  try {
    result = await requestJson(`/app/api/config/test/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: "{}",
    });
  } catch (_) {
    setConfigMessage("测试被拒绝。");
    return;
  }
  if (typeof result.authorization_url === "string") {
    // OAuth 测试要在飞书那边完成一次真实回调，只能整页跳走。
    window.location.assign(result.authorization_url);
    return;
  }
  if (result.status === "failed") {
    setConfigMessage(`测试未通过：${PROBE_ERROR[result.error_code] || "未知原因"}`);
  }
  await loadConfig();
}

document.querySelector("#save-gemini").addEventListener("click", () =>
  saveConfig(providerUpdate("gemini")));
document.querySelector("#save-feishu").addEventListener("click", () =>
  saveConfig(providerUpdate("feishu")));
document.querySelector("#clear-gemini").addEventListener("click", () =>
  clearProvider("gemini"));
document.querySelector("#clear-feishu").addEventListener("click", () =>
  clearProvider("feishu"));
for (const name of CHECK_NAMES) {
  document.querySelector(`#test-${name}`).addEventListener("click", () =>
    runProviderTest(name));
}

async function start() {
  try {
    await loadIdentity();
    // 配置面只对本地管理员开放。其他身份在这里拿到 403，面板保持隐藏——
    // 不是"藏起来但能调"，服务端那一侧同样拒绝。
    try {
      await loadConfig();
    } catch (_) {
      setVisible(configElements.panel, false);
    }
    await loadTasks();
    const requested = new URLSearchParams(window.location.search).get("clarification_parent_task_id");
    if (typeof requested === "string" && requested.length > 0) {
      requestedParentTaskId = requested;
      selectTask(requested);
    } else if (tasks.length > 0) {
      selectTask(tasks[0].task_id);
    }
  } catch (error) {
    if (error.status === 401) {
      window.location.assign("/app");
      return;
    }
    setVisible(elements.listLoading, false);
    setVisible(elements.listError, true);
  }
}

start();
