"""model_copy(update=...) 与 model_construct 必须重新校验或被封死。

Pydantic v2 的原生实现直接写字段、跳过全部校验：Field 约束、model_validator、
AfterValidator 一律不跑。若不覆盖，任何"构造时不可表达"的约束都能被一次 copy
绕过，本项目最核心的取向随之失效。
"""

import datetime as dt
from typing import Self

import pytest
from pydantic import Field, ValidationError, model_validator

from xiaowei_agent.contracts import Channel, Contract, FrozenMap

pytestmark = pytest.mark.security


class _Bounded(Contract):
    n: int = Field(gt=0, strict=True)
    ids: tuple[str, ...] = Field(min_length=1)
    data: FrozenMap

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("duplicate id")
        return self


def _ok() -> _Bounded:
    return _Bounded(n=1, ids=("a",), data={"k": 1})


def test_copy_cannot_bypass_field_constraints() -> None:
    with pytest.raises(ValidationError):
        _ok().model_copy(update={"n": -1})


def test_copy_cannot_bypass_model_validators() -> None:
    with pytest.raises(ValidationError):
        _ok().model_copy(update={"ids": ("x", "x")})


def test_copy_cannot_bypass_min_length() -> None:
    with pytest.raises(ValidationError):
        _ok().model_copy(update={"ids": ()})


def test_copy_cannot_bypass_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        _ok().model_copy(update={"unknown_field": 1})


def test_copy_rewraps_mapping_fields_as_frozen() -> None:
    """写回普通 dict 后必须重新过 FrozenMap，否则可变性从 copy 泄漏回来。"""
    copied = _ok().model_copy(update={"data": {"k": 2}})
    with pytest.raises(TypeError):
        copied.data["k"] = 3  # type: ignore[index]


def test_copy_without_update_is_unchanged() -> None:
    original = _ok()
    assert original.model_copy() == original


def test_valid_copy_still_works() -> None:
    assert _ok().model_copy(update={"n": 5}).n == 5


@pytest.mark.parametrize("sneaky", ["1", True, b"1"])
def test_strict_int_rejects_implicit_coercion(sneaky: object) -> None:
    """lax 模式下 max_tool_calls=True 会被读成 1 并通过 gt=0 —— 边界形同虚设。"""
    with pytest.raises(ValidationError):
        _Bounded(n=sneaky, ids=("a",), data={})


@pytest.mark.parametrize("sneaky", [b"raw", 1, True])
def test_strict_str_rejects_implicit_coercion(sneaky: object) -> None:
    """lax 模式下 str 会接受并解码 bytes，使 id / trace_id 能被二进制填充。"""

    class _WithStr(Contract):
        s: str = Field(strict=True)

    with pytest.raises(ValidationError):
        _WithStr(s=sneaky)


def test_strictness_does_not_break_enum_datetime_or_sequence_inputs() -> None:
    """按字段开 strict 的前提：不得连带打断这三类必需的转换。

    这条是**反向断言**——它防的是"为了更严格而把 strict 提到 model_config"，
    那样 M4 从 TaskStore 反序列化（枚举来自字符串、时间来自 ISO 串、
    序列来自 JSON 数组）会全面失败。
    """

    class _Deserialised(Contract):
        channel: Channel
        at: dt.datetime
        ids: tuple[str, ...]

    got = _Deserialised(channel="cli", at="2026-09-02T00:00:00Z", ids=["a", "b"])
    assert got.channel is Channel.CLI
    assert got.at == dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
    assert got.ids == ("a", "b")


def test_model_construct_is_blocked_on_every_contract() -> None:
    """model_construct 跳过全部校验，是与 model_copy 同类的绕过通道。"""
    with pytest.raises(NotImplementedError):
        _Bounded.model_construct(n=-1, ids=(), data={})


def test_blocking_model_construct_does_not_break_normal_paths() -> None:
    """封死它不得波及 model_validate / model_copy() / model_dump()。"""
    ok = _ok()
    assert _Bounded.model_validate({"n": 2, "ids": ("a",), "data": {}}).n == 2
    assert ok.model_copy() == ok
    assert ok.model_dump()["n"] == 1
