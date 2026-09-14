// 登录壳与强制改密壳共用的最小脚本。
//
// session cookie 是 HttpOnly，脚本读不到，因此 CSRF token 只能来自服务端渲染进
// 页面的 <meta name="csrf-token">。登录那一步本来就没有 session，所以登录页不
// 带 token，服务端对登录也只校验 Origin 与 Content-Type。

const form = document.querySelector("#login-form")
  || document.querySelector("#change-password-form");
const message = document.querySelector("#form-message");
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const submitButton = form === null ? null : form.querySelector("button[type=submit]");

const MESSAGES = Object.freeze({
  400: "提交内容不符合要求，请检查后重试。",
  401: "口令不正确。",
  403: "登录状态已失效，请重新登录。",
  413: "提交内容过长。",
  415: "浏览器未能以 JSON 提交，请刷新页面后重试。",
  422: "新口令至少 12 个字符，请重新设置。",
});

function show(text) {
  message.textContent = text;
  // 与工作台同一套显隐约定：`.is-hidden` 带 !important，不会被后加的排版规则翻掉。
  message.classList.toggle("is-hidden", text.length === 0);
}

async function post(path, body) {
  const headers = { Accept: "application/json", "Content-Type": "application/json" };
  if (csrfMeta !== null) {
    headers["X-CSRF-Token"] = csrfMeta.content;
  }
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify(body),
  });
  if (response.ok) {
    return;
  }
  const error = new Error("request failed");
  error.status = response.status;
  throw error;
}

function describe(error) {
  if (!Number.isInteger(error.status)) {
    return "无法连接到服务，请稍后重试。";
  }
  return MESSAGES[error.status] || "服务暂时不可用，请稍后重试。";
}

async function send(path, body) {
  submitButton.disabled = true;
  show("");
  try {
    await post(path, body);
    // 成功后一律回到 /app：下一张页面由服务端按当前状态决定（改密壳或工作台）。
    window.location.assign("/app");
  } catch (error) {
    show(describe(error));
    submitButton.disabled = false;
  }
}

if (form !== null) {
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (form.id === "login-form") {
      send("/app/api/login", {
        password: document.querySelector("#password").value,
      });
    } else {
      send("/app/api/change-password", {
        current_password: document.querySelector("#current-password").value,
        new_password: document.querySelector("#new-password").value,
      });
    }
  });
}
