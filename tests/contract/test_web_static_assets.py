"""Web 静态资源与 CSP 使用独立、可打包的闭集文件。"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "src" / "xiaowei_agent" / "interfaces" / "web_static"


def test_web_static_asset_set_is_exact_and_not_a_python_package() -> None:
    assert {path.name for path in _STATIC.iterdir()} == {
        "index.html",
        "detail.html",
        "app.css",
        "app.js",
        "detail.js",
        "login.js",
        "login.html",
        "admin.html",
        "admin.js",
    }
    assert not (_STATIC / "__init__.py").exists()


def test_shells_use_only_external_styles_and_scripts() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    detail = (_STATIC / "detail.html").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/app/static/app.css">' in index
    assert '<script type="module" src="/app/static/app.js"></script>' in index
    assert '<link rel="stylesheet" href="/app/static/app.css">' in detail
    assert '<script type="module" src="/app/static/detail.js"></script>' in detail
    assert "<style" not in index + detail
    assert "onclick=" not in (index + detail).lower()
    assert "javascript:" not in (index + detail).lower()
    assert re.findall(r"<script\b([^>]*)>(.*?)</script>", index, re.I | re.S) == [
        (' type="module" src="/app/static/app.js"', "")
    ]
    assert re.findall(r"<script\b([^>]*)>(.*?)</script>", detail, re.I | re.S) == [
        (' type="module" src="/app/static/detail.js"', "")
    ]


def test_desktop_shell_contains_a_static_minimum_width_notice() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    css = (_STATIC / "app.css").read_text(encoding="utf-8")

    assert "请使用宽度至少 1024px 的桌面浏览器" in index
    assert "1023px" in css
    assert "desktop-width-notice" in index


def test_explicit_parent_controls_are_present_and_payload_is_opt_in() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    detail = (_STATIC / "detail.html").read_text(encoding="utf-8")
    app_script = (_STATIC / "app.js").read_text(encoding="utf-8")
    detail_script = (_STATIC / "detail.js").read_text(encoding="utf-8")

    for control_id in (
        'id="continue-task"',
        'id="context-source"',
        'id="clear-context"',
        'id="task-parent"',
    ):
        assert control_id in index
    for control_id in ('id="detail-continue-task"', 'id="detail-parent"'):
        assert control_id in detail
    assert "pendingSubmission.parentTaskId" in app_script
    assert "body.clarification_parent_task_id = pendingSubmission.parentTaskId" in app_script
    assert "encodeURIComponent" in app_script + detail_script
    assert "clarification_parent_task_id: pendingParentTaskId" not in app_script
    assert "innerHTML" not in app_script + detail_script


def test_continue_controls_are_limited_to_clarification_required_tasks() -> None:
    app_script = (_STATIC / "app.js").read_text(encoding="utf-8")
    detail_script = (_STATIC / "detail.js").read_text(encoding="utf-8")

    assert 'task.status === "clarification_required"' in app_script
    assert 'task.status === "clarification_required"' in detail_script
    assert "setVisible(elements.continueTask, terminal)" not in app_script
    assert "setVisible(elements.continueTask, terminal)" not in detail_script


def test_execution_disclosure_is_rendered_as_text_only() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    detail = (_STATIC / "detail.html").read_text(encoding="utf-8")
    app_script = (_STATIC / "app.js").read_text(encoding="utf-8")
    detail_script = (_STATIC / "detail.js").read_text(encoding="utf-8")

    assert 'id="disclosure-block"' in index
    assert 'id="result-disclosure"' in index
    assert 'id="detail-disclosure-block"' in detail
    assert 'id="detail-disclosure"' in detail
    assert "function renderDisclosure(disclosure)" in app_script
    assert "function renderDisclosure(disclosure)" in detail_script
    assert "item.textContent = `${label}：${value}`" in app_script
    assert "item.textContent = `${label}：${value}`" in detail_script


def test_only_the_login_shell_can_enter_the_optional_oauth_route() -> None:
    """飞书可能整个不装配，那时 ``/oauth/feishu/start`` 根本没有注册。

    401 之后跳一条可能不存在的路由，等于把"会话过期"变成 404 死路。唯一在任何
    装配形态下都存在的入口是 ``/app``：它未登录时渲染登录壳，飞书入口渲不渲染
    由服务端按装配结果决定。
    """
    login = (_STATIC / "login.js").read_text(encoding="utf-8")
    protected_scripts = "\n".join(
        (_STATIC / name).read_text(encoding="utf-8")
        for name in ("app.js", "detail.js", "admin.js")
    )

    assert "/oauth/feishu/start" in login
    assert "/oauth/feishu/start" not in protected_scripts
    assert 'window.location.assign("/login?intent=workbench")' in protected_scripts


def test_the_login_script_never_derives_the_csrf_token_itself() -> None:
    """token 只能来自服务端渲染的页面；脚本读不到 HttpOnly cookie。"""
    login = (_STATIC / "login.js").read_text(encoding="utf-8")

    assert "document.cookie" not in login
    assert 'meta[name="csrf-token"]' in login
