"""飞书链接进入的详情 shell 必须保持独立、只读和窄屏可读。"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "src" / "xiaowei_agent" / "interfaces" / "web_static"


def test_detail_shell_has_no_workbench_or_state_change_surface() -> None:
    html = (_STATIC / "detail.html").read_text(encoding="utf-8")
    lowered = html.lower()

    assert "detail.js" in html
    assert "app.js" not in html
    assert "<textarea" not in lowered
    assert "<form" not in lowered
    assert "csrf" not in lowered
    assert "任务列表" not in html
    assert "后台配置" not in html
    assert "发送任务" not in html


def test_detail_javascript_is_read_only_and_clears_revoked_content() -> None:
    script = (_STATIC / "detail.js").read_text(encoding="utf-8")

    assert 'method: "POST"' not in script
    assert "client_submission_id" not in script
    assert "x-csrf-token" not in script
    assert "clearTaskDetail" in script
    assert "document.visibilityState" in script
    assert "30000" in script
    assert "15000" in script


def test_detail_css_has_a_real_narrow_screen_layout() -> None:
    css = (_STATIC / "app.css").read_text(encoding="utf-8")

    assert "@media (max-width: 720px)" in css
    assert ".detail-shell" in css
