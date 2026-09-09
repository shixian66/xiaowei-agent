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

    assert "请使用宽度至少 1280px 的桌面浏览器" in index
    assert "1279px" in css
    assert "desktop-width-notice" in index
