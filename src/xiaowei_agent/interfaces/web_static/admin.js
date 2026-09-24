const grid = document.querySelector("#integration-grid");
const message = document.querySelector("#admin-message");
const feishuCallout = document.querySelector("#feishu-admin-callout");

const LABELS = Object.freeze({ ai: "AI 模型", feishu: "飞书", resources: "运维资源" });
const LOAD = Object.freeze({ unconfigured: "未配置", pending_restart: "等待重启生效", loaded: "已加载", invalid: "加载失败", not_applicable: "尚未接入" });

// 与服务端锁定的常量必须一字不差；安全测试会同 adapter 的真值逐项比较。
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

const DOMAIN_LABELS = Object.freeze({ ai: "AI 配置", feishu: "飞书配置" });

const SERVICE_LABELS = Object.freeze({
  worker: "任务 worker",
  feishu_listener: "飞书 listener",
  channel_worker: "渠道 worker",
  web: "Web",
});

const configElements = Object.freeze({
  panel: document.querySelector("#config-panel"),
  aiGeneration: document.querySelector("#ai-generation"),
  feishuGeneration: document.querySelector("#feishu-generation"),
  aiPending: document.querySelector("#ai-pending"),
  feishuPending: document.querySelector("#feishu-pending"),
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

const identityElements = Object.freeze({
  console: document.querySelector("#identity-console"),
  message: document.querySelector("#identity-message"),
  usersBody: document.querySelector("#identity-users-body"),
  usersEmpty: document.querySelector("#identity-users-empty"),
  usersMore: document.querySelector("#identity-users-more"),
  usersCount: document.querySelector("#identity-users-count"),
  activationsBody: document.querySelector("#identity-activations-body"),
  activationsEmpty: document.querySelector("#identity-activations-empty"),
  activationsMore: document.querySelector("#identity-activations-more"),
  activationsCount: document.querySelector("#identity-activations-count"),
  auditBody: document.querySelector("#identity-audit-body"),
  auditEmpty: document.querySelector("#identity-audit-empty"),
  auditMore: document.querySelector("#identity-audit-more"),
  auditCount: document.querySelector("#identity-audit-count"),
  dialog: document.querySelector("#identity-confirm"),
  form: document.querySelector("#identity-confirm-form"),
  title: document.querySelector("#identity-confirm-title"),
  summary: document.querySelector("#identity-confirm-summary"),
  activationFields: document.querySelector("#identity-activation-fields"),
  activationActor: document.querySelector("#identity-activation-actor"),
  activationName: document.querySelector("#identity-activation-name"),
  activationRole: document.querySelector("#identity-activation-role"),
  confirmMessage: document.querySelector("#identity-confirm-message"),
  cancel: document.querySelector("#identity-confirm-cancel"),
  submit: document.querySelector("#identity-confirm-submit"),
});

const ROLE_LABELS = Object.freeze({ admin: "管理员", operator: "运维人员", user: "普通用户" });
const STATUS_LABELS = Object.freeze({ active: "正常", disabled: "已禁用" });
const SOURCE_LABELS = Object.freeze({ local_admin: "本地管理员", feishu: "飞书" });
const ACTIVATION_SOURCE_LABELS = Object.freeze({ web_login: "Web 登录", safe_task_link: "结果链接", feishu_group: "飞书群" });
const AUDIT_OUTCOME_LABELS = Object.freeze({ started: "进行中", succeeded: "成功", denied: "拒绝", failed: "失败" });
const TARGET_KIND_LABELS = Object.freeze({ user: "用户", activation: "激活申请", duty_binding: "值班绑定", config: "配置", task_content: "任务内容" });
const AUDIT_REASON_LABELS = Object.freeze({ actor_not_admin: "操作人不是管理员", auth_source_not_allowed: "认证来源不允许", target_not_found: "目标不存在", scope_mismatch: "作用域不匹配", conflict: "数据已变化", audit_unwritable: "审计不可写", config_invalid: "配置文件无效", file_io_failed: "配置文件写入失败", probe_failed: "测试未通过", session_invalid: "会话已失效" });

const identityState = {
  users: [],
  usersCursor: null,
  activations: [],
  activationCursor: null,
  audit: [],
  auditCursor: null,
};

let pendingIdentityAction = null;

let csrfToken = null;

function addText(parent, tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  parent.append(node);
}

function text(value, fallback = "—") {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function setVisible(element, visible) {
  element.classList.toggle("is-hidden", !visible);
}

function replaceChildren(element) {
  element.replaceChildren();
}

function auditReasonText(reasonCode) {
  if (reasonCode === null || reasonCode === undefined) return "—";
  return AUDIT_REASON_LABELS[reasonCode] || "未知";
}

function tableCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.textContent = value;
  if (className) cell.className = className;
  row.append(cell);
  return cell;
}

function actionButton(label, action, tone = "button-quiet") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${tone} button-compact`;
  button.textContent = label;
  button.addEventListener("click", () => openIdentityConfirmation(action));
  return button;
}

function formatTime(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? "—" : parsed.toLocaleString("zh-CN", { hour12: false });
}

function identityError(error) {
  if (error.status === 403) return "当前账号没有这项管理权限。";
  if (error.status === 409) return "数据已经变化，页面已刷新，请重新确认。";
  if (error.status === 503) return "身份目录暂时不可用，请稍后重试。";
  return "请求失败，没有写入任何变更。";
}

function setIdentityMessage(value) {
  identityElements.message.textContent = value || "";
  setVisible(identityElements.message, Boolean(value));
}

function effectText(effect) {
  const values = [];
  if (effect && typeof effect.role === "string") values.push(`角色：${ROLE_LABELS[effect.role] || effect.role}`);
  if (effect && typeof effect.status === "string") values.push(`状态：${STATUS_LABELS[effect.status] || effect.status}`);
  return values.length > 0 ? values.join("；") : "—";
}

function renderUsers() {
  replaceChildren(identityElements.usersBody);
  for (const user of identityState.users) {
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    addText(identity, "strong", text(user.display_name));
    addText(identity, "small", text(user.actor), "table-secondary");
    row.append(identity);
    tableCell(row, STATUS_LABELS[user.status] || "未知");
    tableCell(row, ROLE_LABELS[user.role] || "未知");
    tableCell(row, user.feishu_bound === true ? "已绑定" : "未绑定");
    tableCell(row, formatTime(user.updated_at));
    const actions = document.createElement("td");
    actions.className = "table-actions";
    if (user.role === "admin") {
      actions.textContent = "受保护";
    } else {
      const nextStatus = user.status === "active" ? "disabled" : "active";
      actions.append(actionButton(
        nextStatus === "disabled" ? "禁用" : "启用",
        {
          kind: "status",
          title: nextStatus === "disabled" ? "确认禁用用户" : "确认启用用户",
          summary: `${text(user.display_name)}（${text(user.actor)}）`,
          path: "/admin/api/users/status",
          body: {
            user_id: user.user_id,
            expected_status: user.status,
            expected_role: user.role,
            status: nextStatus,
            confirm: true,
          },
        },
      ));
      const nextRole = user.role === "user" ? "operator" : "user";
      const roleButton = actionButton(
        nextRole === "operator" ? "设为运维" : "设为普通用户",
        {
          kind: "role",
          title: "确认变更用户角色",
          summary: `${text(user.display_name)}：${ROLE_LABELS[user.role]} → ${ROLE_LABELS[nextRole]}`,
          path: "/admin/api/users/role",
          body: {
            user_id: user.user_id,
            expected_role: user.role,
            role: nextRole,
            confirm: true,
          },
        },
      );
      roleButton.disabled = user.status !== "active";
      actions.append(roleButton);
    }
    row.append(actions);
    identityElements.usersBody.append(row);
  }
  identityElements.usersCount.textContent = `${identityState.users.length} 人`;
  setVisible(identityElements.usersEmpty, identityState.users.length === 0);
  setVisible(identityElements.usersMore, identityState.usersCursor !== null);
}

function renderActivations() {
  replaceChildren(identityElements.activationsBody);
  for (const request of identityState.activations) {
    const row = document.createElement("tr");
    tableCell(row, text(request.subject_hint));
    tableCell(row, ACTIVATION_SOURCE_LABELS[request.source] || "未知");
    tableCell(row, formatTime(request.requested_at));
    tableCell(row, formatTime(request.expires_at));
    const actions = document.createElement("td");
    actions.className = "table-actions";
    actions.append(actionButton("批准", {
      kind: "approve",
      title: "确认批准激活",
      summary: `${text(request.subject_hint)}。请填写新账号的受控目录字段。`,
      path: "/admin/api/activations/approve",
      body: { request_id: request.request_id, confirm: true },
    }, "button-primary"));
    actions.append(actionButton("拒绝", {
      kind: "reject",
      title: "确认拒绝激活",
      summary: `${text(request.subject_hint)}。拒绝后该申请不能再次处理。`,
      path: "/admin/api/activations/reject",
      body: { request_id: request.request_id, confirm: true },
    }));
    row.append(actions);
    identityElements.activationsBody.append(row);
  }
  identityElements.activationsCount.textContent = `${identityState.activations.length} 条`;
  setVisible(identityElements.activationsEmpty, identityState.activations.length === 0);
  setVisible(identityElements.activationsMore, identityState.activationCursor !== null);
}

function renderAudit() {
  replaceChildren(identityElements.auditBody);
  for (const event of identityState.audit) {
    const row = document.createElement("tr");
    tableCell(row, formatTime(event.created_at));
    tableCell(row, text(event.actor));
    tableCell(row, SOURCE_LABELS[event.auth_source] || "未知");
    tableCell(row, text(event.action));
    tableCell(row, TARGET_KIND_LABELS[event.target_kind] || "未知");
    tableCell(row, AUDIT_OUTCOME_LABELS[event.outcome] || "未知");
    tableCell(row, auditReasonText(event.reason_code));
    tableCell(row, effectText(event.effect));
    identityElements.auditBody.append(row);
  }
  identityElements.auditCount.textContent = `${identityState.audit.length} 条`;
  setVisible(identityElements.auditEmpty, identityState.audit.length === 0);
  setVisible(identityElements.auditMore, identityState.auditCursor !== null);
}

function openIdentityConfirmation(action) {
  pendingIdentityAction = action;
  identityElements.title.textContent = action.title;
  identityElements.summary.textContent = action.summary;
  identityElements.confirmMessage.textContent = "";
  setVisible(identityElements.confirmMessage, false);
  setVisible(identityElements.activationFields, action.kind === "approve");
  identityElements.activationActor.value = "";
  identityElements.activationName.value = "";
  identityElements.activationRole.value = "user";
  identityElements.dialog.showModal();
}

function usersPath() {
  const query = new URLSearchParams({ limit: "50" });
  if (identityState.usersCursor !== null) query.set("after_actor", identityState.usersCursor);
  return `/admin/api/users?${query.toString()}`;
}

function activationsPath() {
  const query = new URLSearchParams({ limit: "50" });
  if (identityState.activationCursor !== null) {
    query.set("before_requested_at", identityState.activationCursor.time);
    query.set("before_request_id", identityState.activationCursor.id);
  }
  return `/admin/api/activations?${query.toString()}`;
}

function auditPath() {
  const query = new URLSearchParams({ limit: "50" });
  if (identityState.auditCursor !== null) {
    query.set("before_created_at", identityState.auditCursor.time);
    query.set("before_event_id", identityState.auditCursor.id);
  }
  return `/admin/api/audit?${query.toString()}`;
}

async function loadUsers(reset = false) {
  if (reset) {
    identityState.users = [];
    identityState.usersCursor = null;
  }
  const page = await requestJson(usersPath());
  identityState.users.push(...page.items);
  identityState.usersCursor = page.next_after_actor || null;
  renderUsers();
}

async function loadActivations(reset = false) {
  if (reset) {
    identityState.activations = [];
    identityState.activationCursor = null;
  }
  const page = await requestJson(activationsPath());
  identityState.activations.push(...page.items);
  identityState.activationCursor = page.next_requested_at && page.next_request_id
    ? { time: page.next_requested_at, id: page.next_request_id }
    : null;
  renderActivations();
}

async function loadAudit(reset = false) {
  if (reset) {
    identityState.audit = [];
    identityState.auditCursor = null;
  }
  const page = await requestJson(auditPath());
  identityState.audit.push(...page.items);
  identityState.auditCursor = page.next_created_at && page.next_event_id
    ? { time: page.next_created_at, id: page.next_event_id }
    : null;
  renderAudit();
}

async function refreshIdentityConsole() {
  await Promise.all([loadUsers(true), loadActivations(true), loadAudit(true)]);
}

async function submitIdentityAction(event) {
  event.preventDefault();
  if (pendingIdentityAction === null) return;
  const body = { ...pendingIdentityAction.body };
  if (pendingIdentityAction.kind === "approve") {
    const actor = identityElements.activationActor.value.trim();
    const displayName = identityElements.activationName.value.trim();
    if (!actor || !displayName) {
      identityElements.confirmMessage.textContent = "请填写用户标识和显示名称。";
      setVisible(identityElements.confirmMessage, true);
      return;
    }
    body.actor = actor;
    body.display_name = displayName;
    body.approved_role = identityElements.activationRole.value;
  }
  identityElements.submit.disabled = true;
  try {
    try {
      await requestJson(pendingIdentityAction.path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(body),
      });
    } catch (error) {
      let conflictRefreshFailed = false;
      if (error.status === 409) {
        try {
          await refreshIdentityConsole();
        } catch (_) {
          conflictRefreshFailed = true;
          setIdentityMessage("数据刷新失败，请刷新页面。");
        }
      }
      identityElements.confirmMessage.textContent = conflictRefreshFailed
        ? "数据已经变化，列表刷新失败，请刷新页面后重新确认。"
        : identityError(error);
      setVisible(identityElements.confirmMessage, true);
      return;
    }
    identityElements.dialog.close();
    pendingIdentityAction = null;
    setIdentityMessage("");
    try {
      await refreshIdentityConsole();
    } catch (_) {
      setIdentityMessage("操作已完成，但列表刷新失败，请刷新页面。");
    }
  } finally {
    identityElements.submit.disabled = false;
  }
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
  if (response.status === 401) {
    window.location.assign("/login?intent=admin_center");
    const error = new Error("unauthorized");
    error.code = "unauthorized";
    throw error;
  }
  if (!response.ok) {
    const error = new Error("request failed");
    error.status = response.status;
    error.code = payload && payload.error ? payload.error.code : "internal_error";
    throw error;
  }
  return payload;
}

function renderDomain(domain) {
  const card = document.createElement("article");
  card.className = "integration-card";
  addText(card, "p", domain.domain.toUpperCase(), "eyebrow");
  addText(card, "h3", LABELS[domain.domain] || domain.domain);
  addText(card, "p", domain.configured ? "已登记配置" : "未登记配置", "integration-primary");
  const load = domain.restart_required ? "pending_restart" : domain.load_status;
  addText(card, "p", LOAD[load] || "状态不可用", "integration-secondary");
  if (domain.domain === "resources") {
    // 资源只有登记，没有测试项：不显示"暂无测试记录"，免得暗示存在一个测试入口。
    addText(card, "p", "已保存，尚未接入", "integration-secondary");
  } else {
    const test = domain.last_test_status === null ? "暂无测试记录" : `最近测试：${domain.last_test_status === "passed" ? "通过" : "失败"}`;
    addText(card, "p", test, "integration-secondary");
  }
  grid.append(card);
}

function setConfigMessage(value) {
  configElements.message.textContent = value || "";
  setVisible(configElements.message, Boolean(value));
}

function renderChecks(checks) {
  for (const name of CHECK_NAMES) {
    const badge = document.querySelector(`#check-${name}`);
    const state = checks[name];
    badge.textContent = DISPLAY_STATE[state] || "—";
    badge.dataset.state = typeof state === "string" ? state : "";
  }
}

function pendingText(services) {
  if (!Array.isArray(services) || services.length === 0) return "";
  return `待重启生效：${services.map((name) => SERVICE_LABELS[name] || name).join("、")}`;
}

function renderPending(element, services) {
  const value = pendingText(services);
  element.textContent = value;
  setVisible(element, value.length > 0);
}

function renderConfig(ai, feishu) {
  configElements.aiGeneration.textContent = `代次 ${ai.generation}`;
  configElements.feishuGeneration.textContent = `代次 ${feishu.generation}`;
  configElements.geminiModel.textContent = GEMINI_FIXED.model;
  configElements.geminiApiVersion.textContent = GEMINI_FIXED.apiVersion;
  configElements.geminiOrigin.textContent = GEMINI_FIXED.origin;
  configElements.feishuCallback.textContent = `${window.location.origin}/oauth/feishu/callback`;
  configElements.geminiConfigured.textContent = ai.gemini.configured ? "已配置" : "未配置";
  configElements.geminiEnabled.checked = ai.gemini.enabled === true;
  configElements.feishuConfigured.textContent = feishu.feishu.configured ? "已配置" : "未配置";
  configElements.feishuEnabled.checked = feishu.feishu.enabled === true;
  configElements.feishuAppId.value = text(feishu.feishu.app_id, "");
  renderPending(configElements.aiPending, ai.pending_restart_services);
  renderPending(configElements.feishuPending, feishu.pending_restart_services);

  // Secret 只进不出；每次渲染清空输入，避免无意把废弃值再次保存。
  configElements.geminiApiKey.value = "";
  configElements.feishuAppSecret.value = "";

  renderChecks({ ...ai.checks, ...feishu.checks });
  for (const name of CHECK_NAMES) {
    const provider = name === "gemini_connection" ? ai.gemini : feishu.feishu;
    document.querySelector(`#test-${name}`).disabled =
      provider.enabled !== true || provider.configured !== true;
  }
}

async function loadConfig() {
  const [ai, feishu] = await Promise.all([
    requestJson("/admin/api/config/ai"),
    requestJson("/admin/api/config/feishu"),
  ]);
  renderConfig(ai, feishu);
  setVisible(configElements.panel, true);
}

function showSaved(saved) {
  // 只点名被写的那一个域：一次保存从不意味着"两域都已保存"。
  const label = DOMAIN_LABELS[saved.domain] || "配置";
  configElements.restartNotice.textContent =
    `${label}已保存为代次 ${saved.generation}。新配置需要在宿主机重启对应进程后才会生效。`;
  setVisible(configElements.restartNotice, saved.restart_required === true);
}

async function saveConfig(domain, body) {
  setConfigMessage("");
  setVisible(configElements.restartNotice, false);
  try {
    const saved = await requestJson(`/admin/api/config/${domain}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body),
    });
    showSaved(saved);
  } catch (error) {
    setConfigMessage(error.code === "unavailable"
      ? "配置暂时无法保存，结果未知；请刷新后核对代次再决定是否重试。"
      : "保存被拒绝，配置未改变。");
  }
  // 结果不确定时也先重读：绝不自动重放写请求。
  try {
    await loadConfig();
  } catch (_) {
    setConfigMessage("配置暂时无法读取，请稍后刷新。");
  }
}

async function clearDomain(domain) {
  const label = DOMAIN_LABELS[domain] || "配置";
  if (!window.confirm(`确认清除${label}？该域的凭据会被删除，其他配置域不受影响。`)) {
    return;
  }
  setConfigMessage("");
  setVisible(configElements.restartNotice, false);
  try {
    const saved = await requestJson(`/admin/api/config/${domain}/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ confirm: true }),
    });
    showSaved(saved);
  } catch (error) {
    setConfigMessage(error.code === "unavailable"
      ? "清除结果未知；请刷新后核对代次。"
      : "清除被拒绝，配置未改变。");
  }
  try {
    await loadConfig();
  } catch (_) {
    setConfigMessage("配置暂时无法读取，请稍后刷新。");
  }
}

function domainUpdate(domain) {
  if (domain === "ai") {
    const update = { enabled: configElements.geminiEnabled.checked };
    if (configElements.geminiApiKey.value.length > 0) {
      update.api_key = configElements.geminiApiKey.value;
    }
    return update;
  }
  const update = { enabled: configElements.feishuEnabled.checked };
  if (configElements.feishuAppId.value.length > 0) {
    update.app_id = configElements.feishuAppId.value;
  }
  if (configElements.feishuAppSecret.value.length > 0) {
    update.app_secret = configElements.feishuAppSecret.value;
  }
  return update;
}

async function runProviderTest(name) {
  setConfigMessage("");
  let result = null;
  try {
    result = await requestJson(`/admin/api/config/test/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: "{}",
    });
  } catch (_) {
    setConfigMessage("测试被拒绝。");
    return;
  }
  if (typeof result.authorization_url === "string") {
    window.location.assign(result.authorization_url);
    return;
  }
  if (result.status === "failed") {
    setConfigMessage(`测试未通过：${PROBE_ERROR[result.error_code] || "未知原因"}`);
  }
  await loadConfig();
}

document.querySelector("#save-gemini").addEventListener("click", () =>
  saveConfig("ai", domainUpdate("ai")));
document.querySelector("#save-feishu").addEventListener("click", () =>
  saveConfig("feishu", domainUpdate("feishu")));
document.querySelector("#clear-gemini").addEventListener("click", () =>
  clearDomain("ai"));
document.querySelector("#clear-feishu").addEventListener("click", () =>
  clearDomain("feishu"));
for (const name of CHECK_NAMES) {
  document.querySelector(`#test-${name}`).addEventListener("click", () =>
    runProviderTest(name));
}
identityElements.usersMore.addEventListener("click", () => loadUsers());
identityElements.activationsMore.addEventListener("click", () => loadActivations());
identityElements.auditMore.addEventListener("click", () => loadAudit());
identityElements.cancel.addEventListener("click", () => {
  pendingIdentityAction = null;
  identityElements.dialog.close();
});
identityElements.form.addEventListener("submit", submitIdentityAction);

// ---------------------------------------------------------------- W4b 资源登记
//
// 只登记参数：这里没有任何测试或连接入口。写入后一律重读，不自动重放写请求。

const RESOURCE_KIND_LABELS = Object.freeze({ starrocks: "StarRocks", prometheus: "Prometheus" });

const resourceElements = Object.freeze({
  panel: document.querySelector("#resources-panel"),
  generation: document.querySelector("#resources-generation"),
  pending: document.querySelector("#resources-pending"),
  notice: document.querySelector("#resources-notice"),
  message: document.querySelector("#resources-message"),
  body: document.querySelector("#resources-body"),
  empty: document.querySelector("#resources-empty"),
  form: document.querySelector("#resource-form"),
  formTitle: document.querySelector("#resource-form-title"),
  id: document.querySelector("#resource-id"),
  kind: document.querySelector("#resource-kind"),
  displayName: document.querySelector("#resource-display-name"),
  environment: document.querySelector("#resource-environment"),
  tlsMode: document.querySelector("#resource-tls-mode"),
  host: document.querySelector("#resource-host"),
  port: document.querySelector("#resource-port"),
  database: document.querySelector("#resource-database"),
  username: document.querySelector("#resource-username"),
  password: document.querySelector("#resource-password"),
  baseUrl: document.querySelector("#resource-base-url"),
  authMode: document.querySelector("#resource-auth-mode"),
  promUsername: document.querySelector("#resource-prom-username"),
  secret: document.querySelector("#resource-secret"),
  enabled: document.querySelector("#resource-enabled"),
  cancel: document.querySelector("#resource-cancel"),
});

function setResourceMessage(value) {
  resourceElements.message.textContent = value || "";
  setVisible(resourceElements.message, Boolean(value));
}

function showKindFields(kind) {
  for (const group of resourceElements.form.querySelectorAll("[data-kind]")) {
    setVisible(group, group.dataset.kind === kind);
  }
}

function setEditOnlyOptions(editing) {
  // "保持不变"只在修改时存在：新建时每个闭集字段都必须显式选择。
  for (const option of resourceElements.form.querySelectorAll("option[data-edit-only]")) {
    option.hidden = !editing;
    option.disabled = !editing;
  }
}

function resetResourceForm() {
  resourceElements.form.reset();
  resourceElements.id.value = "";
  resourceElements.kind.disabled = false;
  resourceElements.formTitle.textContent = "登记新资源";
  setEditOnlyOptions(false);
  resourceElements.environment.value = "dev";
  resourceElements.tlsMode.value = "disabled";
  resourceElements.authMode.value = "none";
  setVisible(resourceElements.cancel, false);
  showKindFields(resourceElements.kind.value);
}

function startResourceEdit(resource) {
  resetResourceForm();
  resourceElements.id.value = resource.resource_id;
  resourceElements.kind.value = resource.kind;
  resourceElements.kind.disabled = true;
  resourceElements.formTitle.textContent = `修改资源：${resource.display_name}`;
  setEditOnlyOptions(true);
  // 修改时空白 = 保持不变；安全投影里本来就没有用户名、数据库、TLS 与凭据。
  resourceElements.environment.value = "";
  resourceElements.tlsMode.value = "";
  resourceElements.authMode.value = "";
  resourceElements.enabled.checked = resource.enabled === true;
  setVisible(resourceElements.cancel, true);
  showKindFields(resource.kind);
  resourceElements.displayName.focus();
}

function resourceAddress(resource) {
  return resource.kind === "starrocks" ? `${resource.host}:${resource.port}` : resource.base_url;
}

function renderResources(view) {
  resourceElements.generation.textContent = `代次 ${view.generation}`;
  renderPending(resourceElements.pending, view.pending_restart_services);
  replaceChildren(resourceElements.body);
  for (const resource of view.resources) {
    const row = document.createElement("tr");
    addText(row, "td", resource.display_name);
    addText(row, "td", RESOURCE_KIND_LABELS[resource.kind] || resource.kind);
    addText(row, "td", resource.environment);
    addText(row, "td", resourceAddress(resource), "resource-address");
    addText(row, "td", resource.configured ? "已配置" : "未配置");
    addText(row, "td", resource.enabled ? "启用" : "停用");
    addText(row, "td", "尚未接入");
    const actions = document.createElement("td");
    actions.className = "resource-actions";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "button button-quiet";
    edit.textContent = "修改";
    edit.addEventListener("click", () => startResourceEdit(resource));
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "button button-quiet";
    clear.textContent = "清除凭据…";
    clear.disabled = resource.configured !== true;
    clear.addEventListener("click", () => confirmResourceAction(resource, "clear"));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button button-quiet";
    remove.textContent = "删除…";
    remove.addEventListener("click", () => confirmResourceAction(resource, "delete"));
    actions.append(edit, clear, remove);
    row.append(actions);
    resourceElements.body.append(row);
  }
  setVisible(resourceElements.empty, view.resources.length === 0);
}

async function loadResources() {
  const view = await requestJson("/admin/api/resources");
  renderResources(view);
  setVisible(resourceElements.panel, true);
}

function showResourceSaved(saved) {
  resourceElements.notice.textContent =
    `资源已保存，运维资源代次 ${saved.generation}。重启任务 worker 后才会被读取；尚未接入任何目标。`;
  setVisible(resourceElements.notice, saved.restart_required === true);
}

async function writeResource(path, body, rejectedText) {
  setResourceMessage("");
  setVisible(resourceElements.notice, false);
  let ok = false;
  try {
    const saved = await requestJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body),
    });
    showResourceSaved(saved);
    ok = true;
  } catch (error) {
    if (error.code === "unavailable") {
      setResourceMessage("结果未知；请刷新后核对代次再决定是否重试。");
    } else if (error.code === "not_found") {
      setResourceMessage("该资源已不存在，列表已刷新。");
    } else if (error.code === "conflict") {
      setResourceMessage("资源编号冲突，未做任何修改，请重试。");
    } else {
      setResourceMessage(rejectedText);
    }
  }
  try {
    await loadResources();
  } catch (_) {
    setResourceMessage("资源列表暂时无法读取，请稍后刷新。");
  }
  return ok;
}

async function confirmResourceAction(resource, action) {
  const prompt = action === "clear"
    ? `确认清除"${resource.display_name}"的凭据？资源本身与其他资源都保留。`
    : `确认删除"${resource.display_name}"？其他资源不受影响。`;
  if (!window.confirm(prompt)) return;
  const path = action === "clear" ? "/admin/api/resources/clear-secret" : "/admin/api/resources/delete";
  await writeResource(path, { resource_id: resource.resource_id, confirm: true }, "操作被拒绝，资源未改变。");
}

function putIfFilled(target, name, value) {
  if (typeof value === "string" && value.length > 0) target[name] = value;
}

function resourceCreateBody(kind) {
  const body = {
    environment: resourceElements.environment.value,
    display_name: resourceElements.displayName.value,
    tls_mode: resourceElements.tlsMode.value,
    enabled: resourceElements.enabled.checked,
  };
  if (kind === "starrocks") {
    body.host = resourceElements.host.value;
    body.port = Number.parseInt(resourceElements.port.value, 10);
    body.database = resourceElements.database.value;
    body.username = resourceElements.username.value;
    body.password = resourceElements.password.value;
    return body;
  }
  body.base_url = resourceElements.baseUrl.value;
  body.auth_mode = resourceElements.authMode.value;
  putIfFilled(body, "username", resourceElements.promUsername.value);
  putIfFilled(body, "secret", resourceElements.secret.value);
  return body;
}

function resourceUpdateBody(kind) {
  // 只带填写过的字段：省略 = 保持不变；从不发送 null 或空串。
  const body = { resource_id: resourceElements.id.value, enabled: resourceElements.enabled.checked };
  putIfFilled(body, "display_name", resourceElements.displayName.value);
  putIfFilled(body, "environment", resourceElements.environment.value);
  putIfFilled(body, "tls_mode", resourceElements.tlsMode.value);
  if (kind === "starrocks") {
    putIfFilled(body, "host", resourceElements.host.value);
    if (resourceElements.port.value.length > 0) {
      body.port = Number.parseInt(resourceElements.port.value, 10);
    }
    putIfFilled(body, "database", resourceElements.database.value);
    putIfFilled(body, "username", resourceElements.username.value);
    putIfFilled(body, "password", resourceElements.password.value);
    return body;
  }
  putIfFilled(body, "base_url", resourceElements.baseUrl.value);
  putIfFilled(body, "auth_mode", resourceElements.authMode.value);
  putIfFilled(body, "username", resourceElements.promUsername.value);
  putIfFilled(body, "secret", resourceElements.secret.value);
  return body;
}

async function submitResource(event) {
  event.preventDefault();
  const kind = resourceElements.kind.value;
  const editing = resourceElements.id.value.length > 0;
  const ok = editing
    ? await writeResource("/admin/api/resources/update", resourceUpdateBody(kind), "修改被拒绝，资源未改变；请检查字段组合。改为无认证前需先使用「清除凭据…」。")
    : await writeResource(`/admin/api/resources/${kind}`, resourceCreateBody(kind), "登记被拒绝，请检查字段组合；未保存任何内容。");
  if (ok) resetResourceForm();
  // Secret 只进不出：无论成败都清空输入。
  resourceElements.password.value = "";
  resourceElements.secret.value = "";
}

resourceElements.kind.addEventListener("change", () => showKindFields(resourceElements.kind.value));
resourceElements.cancel.addEventListener("click", () => resetResourceForm());
resourceElements.form.addEventListener("submit", submitResource);
resetResourceForm();

async function boot() {
  try {
    const [me, status] = await Promise.all([
      requestJson("/app/api/me"),
      requestJson("/admin/api/integration-status"),
    ]);
    csrfToken = me.csrf_token;
    document.querySelector("#admin-actor").textContent = me.actor;
    document.querySelector("#admin-avatar").textContent = me.actor.slice(0, 1).toUpperCase();
    document.querySelector("#admin-source").textContent = me.role === "admin" ? "管理员" : "已认证";
    status.domains.forEach(renderDomain);
    const mayConfigure = me.admin_capabilities.includes("manage_integrations");
    const mayManageUsers = me.admin_capabilities.includes("manage_users");
    const mayViewAudit = me.admin_capabilities.includes("view_admin_audit");
    setVisible(feishuCallout, !mayConfigure);
    setVisible(identityElements.console, mayManageUsers || mayViewAudit);
    if (mayManageUsers || mayViewAudit) {
      const reads = [];
      if (mayManageUsers) reads.push(loadUsers(true), loadActivations(true));
      if (mayViewAudit) reads.push(loadAudit(true));
      await Promise.all(reads);
    }
    if (mayConfigure) {
      await Promise.all([loadConfig(), loadResources()]);
    }
  } catch (error) {
    if (error.code !== "unauthorized") {
      message.textContent = "状态暂时无法读取，请稍后重试。";
      message.classList.remove("is-hidden");
    }
  }
}

boot();
