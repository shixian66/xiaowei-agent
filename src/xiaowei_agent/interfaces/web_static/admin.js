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
const AUDIT_REASON_LABELS = Object.freeze({ actor_not_admin: "操作人不是管理员", auth_source_not_allowed: "认证来源不允许", target_not_found: "目标不存在", scope_mismatch: "作用域不匹配", conflict: "数据已变化", audit_unwritable: "审计不可写" });

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
  const test = domain.last_test_status === null ? "暂无测试记录" : `最近测试：${domain.last_test_status === "passed" ? "通过" : "失败"}`;
  addText(card, "p", test, "integration-secondary");
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

  // Secret 只进不出；每次渲染清空输入，避免无意把废弃值再次保存。
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
  const view = await requestJson("/admin/api/config");
  renderConfig(view);
  setVisible(configElements.panel, true);
}

async function saveConfig(body) {
  setConfigMessage("");
  try {
    const saved = await requestJson("/admin/api/config", {
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
    await requestJson("/admin/api/config/clear", {
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
identityElements.usersMore.addEventListener("click", () => loadUsers());
identityElements.activationsMore.addEventListener("click", () => loadActivations());
identityElements.auditMore.addEventListener("click", () => loadAudit());
identityElements.cancel.addEventListener("click", () => {
  pendingIdentityAction = null;
  identityElements.dialog.close();
});
identityElements.form.addEventListener("submit", submitIdentityAction);

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
      await loadConfig();
    }
  } catch (error) {
    if (error.code !== "unauthorized") {
      message.textContent = "状态暂时无法读取，请稍后重试。";
      message.classList.remove("is-hidden");
    }
  }
}

boot();
