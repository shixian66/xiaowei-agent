const form = document.querySelector("#login-form")
  || document.querySelector("#change-password-form");
const message = document.querySelector("#form-message");
const activationMessage = document.querySelector("#activation-message");
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const submitButton = form === null ? null : form.querySelector("button[type=submit]");

const MESSAGES = Object.freeze({
  400: "提交内容不符合要求，请检查后重试。",
  401: "用户名或密码不正确。",
  403: "当前账号不能进入这个页面。",
  413: "提交内容过长。",
  415: "浏览器未能以 JSON 提交，请刷新页面后重试。",
  422: "新口令至少 12 个字符，请重新设置。",
});

function intentFromLocation() {
  const query = new URLSearchParams(window.location.search);
  const kind = query.get("intent") || "workbench";
  const intent = { kind };
  if (kind === "safe_task_detail") intent.task_id = query.get("task_id");
  if (kind === "activation_status") intent.request_id = query.get("request_id");
  return intent;
}

function show(text) {
  message.textContent = text;
  message.classList.toggle("is-hidden", text.length === 0);
}

function showStatus() {
  if (activationMessage === null) return;
  const notice = new URLSearchParams(window.location.search).get("notice");
  const text = notice === "pending"
    ? "激活申请已提交。审批完成后，请从此页面再次使用飞书登录。"
    : notice === "destination_not_available"
      ? "当前账号不能进入此页面。普通用户请从小维发送的具体结果链接进入。"
      : "";
  activationMessage.textContent = text;
  activationMessage.classList.toggle("is-hidden", text.length === 0);
}

async function post(path, body) {
  const headers = { Accept: "application/json", "Content-Type": "application/json" };
  if (csrfMeta !== null) headers["X-CSRF-Token"] = csrfMeta.content;
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify(body),
  });
  if (response.ok) return response.json();
  const error = new Error("request failed");
  error.status = response.status;
  throw error;
}

function describe(error) {
  if (!Number.isInteger(error.status)) return "无法连接到服务，请稍后重试。";
  return MESSAGES[error.status] || "服务暂时不可用，请稍后重试。";
}

async function send(path, body) {
  submitButton.disabled = true;
  show("");
  try {
    const result = await post(path, body);
    window.location.assign(result.destination);
  } catch (error) {
    show(describe(error));
    submitButton.disabled = false;
  }
}

if (form !== null) {
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (form.id === "login-form") {
      send("/login/api/login", {
        username: document.querySelector("#username").value,
        password: document.querySelector("#password").value,
        return_intent: intentFromLocation(),
      });
    } else {
      send("/login/api/change-password", {
        current_password: document.querySelector("#current-password").value,
        new_password: document.querySelector("#new-password").value,
        return_intent: intentFromLocation(),
      });
    }
  });
}

const oauth = document.querySelector("#feishu-oauth");
if (oauth !== null) {
  const query = new URLSearchParams(window.location.search);
  query.delete("notice");
  oauth.href = `/oauth/feishu/start?${query.toString()}`;
}
showStatus();
