"""受控 PII 字段必须同时关掉两条外泄通道 —— 与 ``Secret*`` 同一套义务，不同理由。

飞书 ``open_id`` 不是凭据：泄露它不会让人登录。但它是"这个人是谁"的外部标识，
进了日志、trace、异常或审计正文就再也收不回来，而这些地方恰好都是排障时最容易
被整段复制粘贴出去的。

义务按**注解名**施加而不是按字段名：``subject_ref`` 今天叫这个名字，明天可能叫
``principal_ref``；而注解是作者写下"这是受控 PII"的地方，义务就挂在那里。这与
``test_secret_field_exposure.py`` 的做法一致，理由也一致——逐个字段补，下一个字段
会以同样方式漏掉。

**断言的是效果，不是写法。** 两个标志可以写在字段上，也可以由 ``ControlledPiiField``
这类别名带进来；测试去问模型"这个字段最终 exclude 了吗"，而不是去数源码里有没有
出现 ``exclude=True``。只认写法时，把标志收进别名（正是 DRY 该做的事）会让这条
用例误报。
"""

import ast
import datetime as dt
import importlib
import pathlib

import pytest

pytestmark = pytest.mark.security

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_PII_ANNOTATION_PREFIX = "ControlledPii"


def controlled_pii_fields() -> list[tuple[str, str, str]]:
    """全部标注为 ``ControlledPii*`` 的模型字段：(模块, 类名, 字段名)。"""
    found: list[tuple[str, str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = "xiaowei_agent." + ".".join(
            path.relative_to(_SRC).with_suffix("").parts
        )
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for stmt in cls.body:
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                    stmt.target, ast.Name
                ):
                    continue
                annotated = {
                    n.id for n in ast.walk(stmt.annotation) if isinstance(n, ast.Name)
                }
                if not any(
                    name.startswith(_PII_ANNOTATION_PREFIX) for name in annotated
                ):
                    continue
                found.append((module, cls.name, stmt.target.id))
    return found


def test_the_scan_actually_finds_the_known_pii_fields() -> None:
    """反空跑：扫不到任何字段时下面那条会空转全绿。

    一次把标记类型改名（``ControlledPii`` -> 别的）就会让扫描归零，而归零的守卫
    看起来和"全部合规"一模一样。
    """
    names = {(cls, field) for _, cls, field in controlled_pii_fields()}
    assert {
        ("ExternalIdentity", "subject_ref"),
        ("BindExternalIdentityCommand", "subject_ref"),
        ("LegacyIdentityMigrationEntry", "subject_ref"),
    } <= names


@pytest.mark.parametrize(
    ("module", "cls_name", "field"),
    controlled_pii_fields(),
    ids=[f"{cls}.{field}" for _, cls, field in controlled_pii_fields()],
)
def test_every_pii_field_is_excluded_and_unrepresented(
    module: str, cls_name: str, field: str
) -> None:
    model = getattr(importlib.import_module(module), cls_name)
    info = model.model_fields[field]
    location = f"{module}:{cls_name}.{field}"
    assert info.exclude is True, f"{location} 缺 exclude=True（model_dump 会带出）"
    assert info.repr is False, f"{location} 缺 repr=False（repr/str 会带出）"


def test_pii_never_reaches_repr_or_dump() -> None:
    """行为对照：静态标志对了，实际输出也必须对。

    只断标志时，一个把 ``__repr__`` 重写回去的子类仍会全绿——而 Pydantic 的
    ``repr=False`` 只管默认实现。
    """
    from xiaowei_agent.contracts import IdentitySource
    from xiaowei_agent.contracts.identity import (
        BindExternalIdentityCommand,
        ExternalIdentity,
    )

    open_id = "ou_" + "1a2b3c4d5e6f7a8b"
    now = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)
    samples = (
        ExternalIdentity(
            user_id="u-1",
            provider=IdentitySource.FEISHU,
            tenant_id="t-1",
            environment_id="dev",
            subject_ref=open_id,
            created_at=now,
            last_seen_at=now,
        ),
        BindExternalIdentityCommand(
            user_id="u-1",
            tenant_id="t-1",
            environment_id="dev",
            subject_ref=open_id,
        ),
    )
    for sample in samples:
        rendered = repr(sample) + str(sample) + sample.model_dump_json()
        assert open_id not in rendered, type(sample).__name__
        assert "subject_ref" not in sample.model_dump()
