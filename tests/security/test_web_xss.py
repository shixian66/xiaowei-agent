"""浏览器动态内容必须保持纯文本，不获得 HTML/脚本解释权。"""

import datetime as dt
from pathlib import Path

import pytest
from tests.contract.test_web_task_api import _Access, _client

from xiaowei_agent.application.channel_access import AccessibleTask
from xiaowei_agent.contracts import (
    RenderPayload,
    RenderSection,
    TaskStatus,
    TaskView,
    task_query_path,
)

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "src" / "xiaowei_agent" / "interfaces" / "web_static"


def test_javascript_has_no_html_execution_sink() -> None:
    # 按目录取而不是按文件名列举：新增一个脚本不应该悄悄落在这条守卫之外。
    paths = sorted(_STATIC.glob("*.js"))
    assert len(paths) >= 3
    scripts = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    )

    assert all(item not in scripts for item in forbidden)
    assert ".textContent" in scripts


def test_static_html_has_no_inline_handlers_or_template_interpolation() -> None:
    paths = sorted(_STATIC.glob("*.html"))
    assert len(paths) >= 2
    html = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "{{" not in html
    assert "{%" not in html
    assert " onerror=" not in html.lower()
    assert " onload=" not in html.lower()
    assert " onclick=" not in html.lower()


def test_admin_identity_assets_never_open_an_html_or_dynamic_code_sink() -> None:
    admin = (_STATIC / "admin.html").read_text(encoding="utf-8")
    script = (_STATIC / "admin.js").read_text(encoding="utf-8")

    assert '<script type="module" src="/app/static/admin.js"></script>' in admin
    for forbidden in (
        "innerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in script


async def test_untrusted_render_text_remains_json_data_not_html_response() -> None:
    injected = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    view = TaskView(
        task_id="task-xss",
        status=TaskStatus.SUCCEEDED,
        render=RenderPayload(
            answer=injected,
            sections=(RenderSection(title=injected, body=injected, refs=(injected,)),),
            next_steps=(injected,),
            status=TaskStatus.SUCCEEDED,
            refs=(injected,),
        ),
        query_path=task_query_path("task-xss"),
    )
    access = _Access()
    access.detail_result = AccessibleTask(
        task_view=view,
        request_preview=injected,
        submitted_at=dt.datetime(2026, 9, 9, tzinfo=dt.UTC),
        task_version=2,
    )
    client, _, _ = _client(access=access)

    async with client:
        response = await client.get("/app/api/tasks/task-xss")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["render"]["answer"] == injected
    assert "<script>alert(2)</script>" in response.text


async def test_model_advisory_remains_plain_json_data() -> None:
    injected = '[运行命令](javascript:alert(1)) <img src=x onerror="alert(2)">'
    view = TaskView(
        task_id="task-model-xss",
        status=TaskStatus.SUCCEEDED,
        render=RenderPayload(
            answer="确定性结论",
            sections=(
                RenderSection(
                    title="模型分析（仅供参考）",
                    body=injected,
                    refs=(),
                ),
            ),
            next_steps=(),
            status=TaskStatus.SUCCEEDED,
            refs=(),
        ),
        query_path=task_query_path("task-model-xss"),
    )
    access = _Access()
    access.detail_result = AccessibleTask(
        task_view=view,
        request_preview="模型安全展示",
        submitted_at=dt.datetime(2026, 9, 9, tzinfo=dt.UTC),
        task_version=2,
    )
    client, _, _ = _client(access=access)

    async with client:
        response = await client.get("/app/api/tasks/task-model-xss")

    assert response.status_code == 200
    section = response.json()["render"]["sections"][0]
    assert section == {"title": "模型分析（仅供参考）", "body": injected, "refs": []}
