const grid = document.querySelector("#integration-grid");
const message = document.querySelector("#admin-message");
const localCallout = document.querySelector("#local-admin-callout");
const feishuCallout = document.querySelector("#feishu-admin-callout");

const LABELS = Object.freeze({ ai: "AI 模型", feishu: "飞书", resources: "运维资源" });
const LOAD = Object.freeze({ unconfigured: "未配置", pending_restart: "等待重启生效", loaded: "已加载", invalid: "加载失败", not_applicable: "尚未接入" });

function addText(parent, tag, value, className) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (className) node.className = className;
  parent.append(node);
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

async function readJson(path) {
  const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (response.status === 401) {
    window.location.assign("/login?intent=admin_center");
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error("request failed");
  return response.json();
}

async function boot() {
  try {
    const [me, status] = await Promise.all([readJson("/app/api/me"), readJson("/admin/api/integration-status")]);
    document.querySelector("#admin-actor").textContent = me.actor;
    document.querySelector("#admin-avatar").textContent = me.actor.slice(0, 1).toUpperCase();
    document.querySelector("#admin-source").textContent = me.role === "admin" ? "管理员" : "已认证";
    status.domains.forEach(renderDomain);
    const mayConfigure = me.admin_capabilities.includes("manage_integrations");
    localCallout.classList.toggle("is-hidden", !mayConfigure);
    feishuCallout.classList.toggle("is-hidden", mayConfigure);
  } catch (error) {
    if (error.message !== "unauthorized") {
      message.textContent = "状态暂时无法读取，请稍后重试。";
      message.classList.remove("is-hidden");
    }
  }
}

boot();
