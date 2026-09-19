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
  loading: document.querySelector("#detail-loading"),
  unavailable: document.querySelector("#detail-unavailable"),
  errorMessage: document.querySelector("#detail-error-message"),
  retry: document.querySelector("#detail-retry"),
  content: document.querySelector("#detail-content"),
  status: document.querySelector("#detail-status"),
  polling: document.querySelector("#detail-polling"),
  request: document.querySelector("#detail-request"),
  taskId: document.querySelector("#detail-task-id"),
  parentRow: document.querySelector("#detail-parent-row"),
  parent: document.querySelector("#detail-parent"),
  continueTask: document.querySelector("#detail-continue-task"),
  submittedAt: document.querySelector("#detail-submitted-at"),
  version: document.querySelector("#detail-version"),
  timelineStatus: document.querySelector("#detail-timeline-status"),
  progress: document.querySelector("#detail-progress"),
  result: document.querySelector("#detail-result"),
  answer: document.querySelector("#detail-answer"),
  sections: document.querySelector("#detail-sections"),
  nextBlock: document.querySelector("#detail-next-block"),
  nextSteps: document.querySelector("#detail-next-steps"),
  refsBlock: document.querySelector("#detail-refs-block"),
  refs: document.querySelector("#detail-refs"),
});

let pollTimer = null;
let pollDelay = 5000;
let stopped = false;

function text(value, fallback = "—") {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function setVisible(element, visible) {
  element.classList.toggle("is-hidden", !visible);
}

function statusInfo(status) {
  return STATUS[status] || { label: "未知状态", icon: "?" };
}

function setStatus(status) {
  const presentation = statusInfo(status);
  elements.status.replaceChildren();
  const icon = document.createElement("span");
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = presentation.icon;
  const label = document.createElement("span");
  label.textContent = presentation.label;
  elements.status.append(icon, label);
  elements.status.dataset.status = status;
  elements.timelineStatus.textContent = presentation.label;
}

function formatTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function clearSafeResult() {
  elements.answer.textContent = "";
  elements.sections.replaceChildren();
  elements.nextSteps.replaceChildren();
  elements.refs.replaceChildren();
  setVisible(elements.nextBlock, false);
  setVisible(elements.refsBlock, false);
  setVisible(elements.result, false);
}

function clearTaskDetail() {
  window.clearTimeout(pollTimer);
  elements.request.textContent = "";
  elements.taskId.textContent = "";
  elements.parent.textContent = "";
  elements.parent.href = "/app";
  elements.continueTask.href = "/app";
  elements.submittedAt.textContent = "";
  elements.version.textContent = "";
  elements.timelineStatus.textContent = "";
  clearSafeResult();
  setVisible(elements.progress, false);
  setVisible(elements.parentRow, false);
  setVisible(elements.continueTask, false);
  setVisible(elements.content, false);
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
  clearSafeResult();
  if (!render || typeof render !== "object") {
    return;
  }
  elements.answer.textContent = text(render.answer, "结果未提供结论");
  if (Array.isArray(render.sections)) {
    for (const section of render.sections) {
      const block = document.createElement("section");
      block.className = "result-section";
      const title = document.createElement("h3");
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

function renderTask(task) {
  setVisible(elements.loading, false);
  setVisible(elements.unavailable, false);
  setVisible(elements.content, true);
  setStatus(task.status);
  elements.request.textContent = text(task.request_preview, "未提供任务摘要");
  elements.taskId.textContent = text(task.task_id);
  const parentTaskId = typeof task.clarification_parent_task_id === "string" && task.clarification_parent_task_id.length > 0
    ? task.clarification_parent_task_id
    : null;
  elements.parent.textContent = parentTaskId || "";
  elements.parent.href = parentTaskId === null
    ? "/app"
    : `/app/tasks/${encodeURIComponent(parentTaskId)}`;
  setVisible(elements.parentRow, parentTaskId !== null);
  elements.submittedAt.textContent = formatTime(task.submitted_at);
  elements.version.textContent = Number.isInteger(task.task_version) ? String(task.task_version) : "—";
  const terminal = TERMINAL_STATUSES.has(task.status);
  const canContinue = task.status === "clarification_required";
  elements.continueTask.href = canContinue
    ? `/app?clarification_parent_task_id=${encodeURIComponent(task.task_id)}`
    : "/app";
  setVisible(elements.continueTask, canContinue);
  setVisible(elements.progress, !terminal);
  if (terminal) {
    elements.polling.textContent = "已停止同步";
    renderSafeResult(task.render);
  } else {
    elements.polling.textContent = document.visibilityState === "hidden" ? "后台低频同步" : "自动同步";
    clearSafeResult();
  }
  return terminal;
}

function currentTaskId() {
  const marker = "/app/tasks/";
  if (!window.location.pathname.startsWith(marker)) {
    return null;
  }
  const encoded = window.location.pathname.slice(marker.length);
  if (!encoded || encoded.includes("/")) {
    return null;
  }
  try {
    return decodeURIComponent(encoded);
  } catch (_) {
    return null;
  }
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  if (stopped) {
    return;
  }
  const delay = document.visibilityState === "hidden" ? Math.max(30000, pollDelay) : pollDelay;
  pollTimer = window.setTimeout(readTask, delay);
  pollDelay = Math.min(15000, Math.max(5000, Math.round(pollDelay * 1.6)));
}

async function readTask() {
  const taskId = currentTaskId();
  if (taskId === null) {
    showUnavailable("任务链接无效。", true);
    return;
  }
  window.clearTimeout(pollTimer);
  setVisible(elements.unavailable, false);
  setVisible(elements.loading, elements.content.classList.contains("is-hidden"));
  try {
    const response = await fetch(`/app/api/tasks/${encodeURIComponent(taskId)}`, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      const error = new Error("detail read failed");
      error.status = response.status;
      throw error;
    }
    const task = await response.json();
    if (renderTask(task)) {
      stopped = true;
      return;
    }
  } catch (error) {
    clearTaskDetail();
    const unavailable = error.status === 404 || error.status === 401;
    showUnavailable(
      unavailable
        ? "任务不存在、当前无权查看，或群成员权限已经变化。"
        : "暂时无法读取任务；这不代表任务已经失败。",
      unavailable,
    );
    if (unavailable) {
      stopped = true;
      return;
    }
  }
  schedulePoll();
}

function showUnavailable(message, terminal) {
  setVisible(elements.loading, false);
  setVisible(elements.unavailable, true);
  elements.errorMessage.textContent = message;
  stopped = terminal;
}

elements.retry.addEventListener("click", () => {
  stopped = false;
  pollDelay = 5000;
  setVisible(elements.unavailable, false);
  setVisible(elements.loading, true);
  readTask();
});

document.addEventListener("visibilitychange", () => {
  if (stopped) {
    return;
  }
  if (document.visibilityState === "visible") {
    pollDelay = 5000;
    readTask();
  } else {
    schedulePoll();
  }
});

readTask();
