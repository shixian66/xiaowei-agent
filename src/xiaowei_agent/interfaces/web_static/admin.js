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
    setVisible(feishuCallout, !mayConfigure);
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
