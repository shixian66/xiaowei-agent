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
    admin = (_STATIC / "admin.html").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/app/static/app.css">' in index
    assert '<script type="module" src="/app/static/app.js"></script>' in index
    assert '<link rel="stylesheet" href="/app/static/app.css">' in detail
    assert '<script type="module" src="/app/static/detail.js"></script>' in detail
    assert '<link rel="stylesheet" href="/app/static/app.css">' in admin
    assert '<script type="module" src="/app/static/admin.js"></script>' in admin
    assert "/app/static/app.js" not in admin
    assert "<style" not in index + detail + admin
    assert "onclick=" not in (index + detail + admin).lower()
    assert "javascript:" not in (index + detail + admin).lower()
    assert re.findall(r"<script\b([^>]*)>(.*?)</script>", index, re.I | re.S) == [
        (' type="module" src="/app/static/app.js"', "")
    ]
    assert re.findall(r"<script\b([^>]*)>(.*?)</script>", detail, re.I | re.S) == [
        (' type="module" src="/app/static/detail.js"', "")
    ]
    assert re.findall(r"<script\b([^>]*)>(.*?)</script>", admin, re.I | re.S) == [
        (' type="module" src="/app/static/admin.js"', "")
    ]


def test_raw_configuration_ui_belongs_only_to_the_admin_shell() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    admin = (_STATIC / "admin.html").read_text(encoding="utf-8")
    app_script = (_STATIC / "app.js").read_text(encoding="utf-8")
    admin_script = (_STATIC / "admin.js").read_text(encoding="utf-8")

    for forbidden in (
        'id="config-panel"',
        'id="gemini-api-key"',
        'id="feishu-app-secret"',
    ):
        assert forbidden not in index
        assert forbidden in admin
    assert "/app/api/config" not in app_script + admin_script
    assert "/admin/api/config" not in app_script
    for required in (
        '"/admin/api/config/ai"',
        '"/admin/api/config/feishu"',
        "`/admin/api/config/${domain}`",
        "`/admin/api/config/${domain}/clear`",
        "/admin/api/config/test/",
    ):
        assert required in admin_script
    # W4a 不再有跨域的整体读写/清除入口：一次保存只能动一个配置域。
    for retired in ('"/admin/api/config"', '"/admin/api/config/clear"'):
        assert retired not in admin_script


def test_admin_shell_contains_the_three_lite_identity_regions_and_one_confirmation() -> None:
    admin = (_STATIC / "admin.html").read_text(encoding="utf-8")
    script = (_STATIC / "admin.js").read_text(encoding="utf-8")

    for required in (
        'id="identity-users"',
        'id="identity-activations"',
        'id="identity-audit"',
        'id="identity-confirm"',
        'id="identity-confirm-submit"',
        'id="identity-confirm-cancel"',
    ):
        assert required in admin
    assert admin.count('<dialog id="identity-confirm"') == 1
    assert "用户与权限" in admin
    assert "待激活申请" in admin
    assert "管理审计" in admin
    for column in ("目标类型", "结果", "原因", "影响"):
        assert f"<th>{column}</th>" in admin
    assert 'TARGET_KIND_LABELS[event.target_kind] || "未知"' in script
    assert 'AUDIT_OUTCOME_LABELS[event.outcome] || "未知"' in script
    assert "auditReasonText(event.reason_code)" in script
    assert "event.reason_code || AUDIT_OUTCOME_LABELS" not in script
    assert "操作已完成，但列表刷新失败，请刷新页面。" in script
    assert 'setIdentityMessage("操作已完成，但列表刷新失败，请刷新页面。")' in script
    assert "数据已经变化，列表刷新失败，请刷新页面后重新确认。" in script


def test_admin_script_uses_only_the_governed_identity_route_closure() -> None:
    script = (_STATIC / "admin.js").read_text(encoding="utf-8")

    required = {
        "/admin/api/users",
        "/admin/api/users/status",
        "/admin/api/users/role",
        "/admin/api/activations",
        "/admin/api/activations/approve",
        "/admin/api/activations/reject",
        "/admin/api/audit",
    }
    assert all(path in script for path in required)
    assert "createElement" in script
    assert ".textContent" in script
    assert "subject_ref" not in script
    assert "open_id" not in script
    assert "target_ref_digest" not in script


def test_admin_console_keeps_raw_configuration_out_of_the_workbench_script() -> None:
    workbench = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert "/admin/api/" not in workbench


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


def _resources_panel(admin: str) -> str:
    start = admin.find('id="resources-panel"')
    assert start >= 0, "管理页缺少资源登记区"
    end = admin.find("</section>", start)
    assert end > start
    return admin[start:end]


def test_the_resources_panel_says_saved_but_not_connected_and_has_no_test_button() -> None:
    """W4b 只登记参数：页面必须说清"尚未接入"，也不能出现任何测试/连接按钮。"""
    admin = (_STATIC / "admin.html").read_text(encoding="utf-8")
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    panel = _resources_panel(admin)
    assert "已保存，尚未接入" in panel
    assert 'id="resources-generation"' in panel
    assert 'id="resources-pending"' in panel
    for forbidden in ("测试", "连接测试", "测试连接", "test-", "check-"):
        assert forbidden not in panel, forbidden
    assert 'id="resources-panel"' not in index


def test_the_resources_script_only_uses_the_registration_routes() -> None:
    admin_script = (_STATIC / "admin.js").read_text(encoding="utf-8")
    app_script = (_STATIC / "app.js").read_text(encoding="utf-8")
    for required in (
        '"/admin/api/resources"',
        "`/admin/api/resources/${kind}`",
        '"/admin/api/resources/update"',
        '"/admin/api/resources/clear-secret"',
        '"/admin/api/resources/delete"',
    ):
        assert required in admin_script, required
    assert "/admin/api/resources" not in app_script
    # 没有资源测试或连接入口：W4c 才决定真实调用路径。
    for forbidden in ("/admin/api/resources/test", "resources/probe", "resources/connect"):
        assert forbidden not in admin_script


def test_login_shell_without_oauth_does_not_mention_feishu() -> None:
    from xiaowei_agent.interfaces.web_app import _login_shell

    shell = (_STATIC / "login.html").read_text(encoding="utf-8")
    off = _login_shell(shell=shell, oauth_available=False)
    on = _login_shell(shell=shell, oauth_available=True)

    intro_off = re.search(r'<p class="auth-intro">(.*?)</p>', off)
    assert intro_off is not None
    assert "飞书" not in intro_off.group(1)
    assert intro_off.group(1) == "请使用本地管理员账号登录。"
    assert '<div class="oauth-entry is-hidden" id="oauth-entry">' in off
    # 装配了 OAuth 时，登录页与静态壳逐字一致。
    assert on == shell


def test_login_shell_markup_drift_fails_at_startup() -> None:
    import pytest

    from xiaowei_agent.interfaces.web_app import _login_shell

    shell = (_STATIC / "login.html").read_text(encoding="utf-8")
    without_intro = re.sub(r'<p class="auth-intro">.*?</p>', "", shell)
    for drifted in ("<main></main>", without_intro):
        with pytest.raises(RuntimeError, match="login shell markup drifted"):
            _login_shell(shell=drifted, oauth_available=False)


def test_workbench_links_to_the_admin_center_only_by_capability() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    script = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert (
        '<a class="button button-quiet is-hidden" id="admin-center-link" '
        'href="/admin">管理中心</a>'
    ) in index
    assert 'document.querySelector("#admin-center-link")' in script
    assert 'capabilities.includes("view_integration_status")' in script
    # 入口只是展示，不绕过服务端：脚本不直接读取任何 /admin/api 路由。
    assert "/admin/api/" not in script
