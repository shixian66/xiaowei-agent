"""目标必须稳定且非空，规范化后碰撞一律拒绝。

碰撞**拒绝**而非静默合并：两个规范化后相同的资源 ID 说明调用方传入了未确认的
别名，此时"目标解析不稳定"，不能生成可审批的指纹（ARCHITECTURE §7.2）。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ResolvedTarget

pytestmark = pytest.mark.security


def _target(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "provider": "starrocks",
        "resource_kind": "cluster",
        "resource_ids": ("c1", "c2"),
        "selector_version": "1",
    }
    return base | overrides


def test_empty_resource_ids_are_rejected() -> None:
    """空目标无法生成可审批指纹。"""
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=()))


def test_exact_duplicate_resource_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=("c1", "c1")))


def test_nfc_colliding_resource_ids_are_rejected() -> None:
    """组合式与预组合式写法指向同一资源：这是未确认别名，拒绝而非合并。

    **所有 NFC 敏感字面量一律写显式码点**（"cafe\\u0301" 而非直接敲重音字母）：
    编辑器、格式化工具与复制粘贴都可能把源码里的重音字母悄悄归一为预组合形式，
    两个字面量就变成同一个串——assert a != b 直接失败，或者更糟，dict 字面量
    塌成一项让测试恒过。这不是风格问题，是测试是否真的在测。
    """
    decomposed = "cafe\u0301"  # c a f e + U+0301 组合尖音符
    precomposed = "caf\u00e9"  # 预组合式
    assert decomposed != precomposed
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=(decomposed, precomposed)))


def test_resource_ids_are_normalised_on_construction() -> None:
    """构造后取值即为 NFC 形式，后续 hash 无需再关心输入写法。"""
    target = ResolvedTarget(**_target(resource_ids=("cafe\u0301",)))
    assert target.resource_ids == ("caf\u00e9",)


def test_normalisation_survives_a_copy() -> None:
    """规范化对 copy 同样生效，且对已规范化的值幂等。

    ``model_copy`` 会重新走完整校验，因此字段级 AfterValidator 会再跑一次；
    它必须对已是 NFC 的输入原样返回，而不是再动一次或报碰撞。
    """
    target = ResolvedTarget(**_target(resource_ids=("cafe\u0301",)))
    again = target.model_copy(update={"selector_version": "2"})
    assert again.resource_ids == ("caf\u00e9",)


@pytest.mark.parametrize("bad", ["", "  ", " c1 "])
def test_blank_or_padded_resource_id_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=("c1", bad)))


def test_copy_cannot_create_colliding_or_empty_resource_ids() -> None:
    target = ResolvedTarget(**_target())
    with pytest.raises(ValidationError):
        target.model_copy(update={"resource_ids": ("cafe\u0301", "caf\u00e9")})
    with pytest.raises(ValidationError):
        target.model_copy(update={"resource_ids": ()})
