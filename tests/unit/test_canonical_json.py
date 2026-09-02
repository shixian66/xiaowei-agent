"""canonical_json 的稳定性来自五条规则。

键序、无空白、NFC、拒绝不可表达值、**NFC 后键碰撞即抛错**。
"""

import math

import pytest

from xiaowei_agent.planning import canonical_json


def test_key_order_is_independent_of_insertion_order() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_no_whitespace_and_utf8() -> None:
    assert canonical_json({"k": "值"}) == '{"k":"值"}'.encode()


def test_strings_are_nfc_normalised() -> None:
    decomposed = "e\u0301"  # e + U+0301，显式码点
    precomposed = "\u00e9"  # é
    assert decomposed != precomposed
    assert canonical_json({"k": decomposed}) == canonical_json({"k": precomposed})


def test_canonical_json_rejects_key_collision_after_nfc() -> None:
    """两个不同键规范化后相同，若静默覆盖会让两份不同数据产生同一指纹。

    与 ResolvedTarget 的 resource_ids 是同一类缺陷，这一层也必须堵住。
    """
    with pytest.raises(ValueError, match="collision"):
        canonical_json({"e\u0301": 1, "\u00e9": 2})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"k": bad})


def test_unsupported_type_is_rejected() -> None:
    """不做 str() 兜底：静默字符串化会让两个不同对象产生同一指纹。"""
    with pytest.raises(TypeError):
        canonical_json({"k": object()})


def test_non_string_key_is_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_json({1: "a"})


def test_bytes_are_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_json({"k": b"raw"})


def test_integral_float_and_int_stay_distinct() -> None:
    assert canonical_json({"k": 1}) != canonical_json({"k": 1.0})


def test_negative_zero_is_normalised_to_zero() -> None:
    """-0.0 == 0.0 为真，但 json.dumps 分别产出 "-0.0" 与 "0.0"。

    两个**相等**的取值得到不同指纹，与 NFC 未规范化是同一类不稳定。
    """
    assert canonical_json({"k": -0.0}) == canonical_json({"k": 0.0})
    assert canonical_json({"k": -0.0}) == b'{"k":0.0}'
