"""凭据字段必须同时关掉两条外泄通道 —— **这条测试针对的是根因，不是某两个字段**。

RI5 的 `IntegrationConfig` 一开始只写了 ``exclude=True``，四条基线全绿，而
``repr(config)`` 会把完整的 Gemini Key 和飞书 App Secret 原样打印出来。成因不是
"忘了一个参数"，而是 Pydantic 有**两条**独立的外泄通道：

- ``exclude=True`` 只作用于 ``model_dump()`` / ``model_dump_json()``；
- ``repr()`` 与 ``str()`` 走 ``repr=False``，不受 ``exclude`` 影响。

写的时候挡住其中一条不会有任何反馈，当时的用例甚至断言了 ``repr(dumped)``
（已经 dump 过的字典）而不是 ``repr(config)``——看起来在测 repr，实际什么也没测。
逐个补字段治不了这个：下一个 secret 字段会以同样方式漏掉。

因此这里按**类型**而不是按字段名施加义务：凡是标注为 ``Secret*`` 的字段，
两个标志必须齐全。按字段名做不到——`idempotency_key`、`tenant_key`、`reason_key`
都不是凭据，而 `password_hash` 又不以 `key`/`secret` 结尾，任何名字模式要么误杀
要么漏网。类型是作者写下"这是凭据"的地方，义务就挂在那里。
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.security

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"


def _contract_classes(tree: ast.Module) -> set[str]:
    """同一文件内 ``Contract`` 子类的传递闭包。"""
    kinds = {"Contract"}
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for _ in range(len(classes) + 1):
        for cls in classes:
            if {b.id for b in cls.bases if isinstance(b, ast.Name)} & kinds:
                kinds.add(cls.name)
    return kinds


def _field_flags(value: ast.expr | None) -> dict[str, object]:
    """从 ``Field(...)`` 调用里取出关键字实参；不是 ``Field(...)`` 就返回空。"""
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
        return {}
    if value.func.id != "Field":
        return {}
    return {
        keyword.arg: keyword.value.value
        for keyword in value.keywords
        if keyword.arg is not None and isinstance(keyword.value, ast.Constant)
    }


def secret_fields() -> list[tuple[str, str, str, dict[str, object]]]:
    """全部标注为 ``Secret*`` 的契约字段：(文件, 类名, 字段名, Field 关键字)。"""
    found: list[tuple[str, str, str, dict[str, object]]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        kinds = _contract_classes(tree)
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if not ({b.id for b in cls.bases if isinstance(b, ast.Name)} & kinds):
                continue
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                    stmt.target, ast.Name
                ):
                    continue
                annotated = {
                    n.id for n in ast.walk(stmt.annotation) if isinstance(n, ast.Name)
                }
                if not any(name.startswith("Secret") for name in annotated):
                    continue
                found.append(
                    (
                        str(path.relative_to(_SRC)),
                        cls.name,
                        stmt.target.id,
                        _field_flags(stmt.value),
                    )
                )
    return found


def test_the_scan_actually_finds_the_known_secret_fields() -> None:
    """反空跑：扫不到任何字段时上面那条会空转全绿。"""
    names = {(cls, field) for _, cls, field, _ in secret_fields()}
    assert {
        ("GeminiIntegration", "api_key"),
        ("FeishuIntegration", "app_secret"),
        ("LocalAdminRecord", "password_hash"),
        ("ChangePasswordCommand", "password_hash"),
    } <= names


@pytest.mark.parametrize(
    ("location", "flags"),
    [
        (f"{path}:{cls}.{field}", flags)
        for path, cls, field, flags in secret_fields()
    ],
    ids=[f"{cls}.{field}" for _, cls, field, _ in secret_fields()],
)
def test_every_secret_field_closes_both_exposure_channels(
    location: str, flags: dict[str, object]
) -> None:
    assert flags.get("exclude") is True, f"{location} 缺 exclude=True（model_dump 会带出）"
    assert flags.get("repr") is False, f"{location} 缺 repr=False（repr/str 会带出）"
