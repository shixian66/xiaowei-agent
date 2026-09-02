"""``safe_exception_text`` 的两条契约：永不再抛，且已脱敏。

分开直接测这个函数，而不是只经 Gateway 间接覆盖。Gateway 目前把 ``ExternalContent``
用完即弃（只留 digest 作 ``cause_ref``），因此"脱敏"这一半在下游看不出差别——
变异测试已确认去掉 ``scrub_text`` 时经 Gateway 的用例仍全绿。M3 会开始持久化
``ExternalContent``，届时脱敏就直接决定 secret 是否落库；契约必须在它自己的
层面被钉死，不能依赖某个调用方碰巧丢弃了结果。
"""

import pytest

from xiaowei_agent.redaction import REDACTED, safe_exception_text

pytestmark = pytest.mark.security

CANARY = "hunter2"


class _RaisingStrError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError(f"str failed: password={CANARY}")


class _RaisingBothError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("str failed")

    def __repr__(self) -> str:
        raise RuntimeError("repr failed")


class _BaseExceptionStrError(RuntimeError):
    """``__str__`` 抛 ``BaseException`` 而不是 ``Exception``。

    只捕 ``Exception`` 时这条路径仍能打穿边界：``__str__`` 抛 ``KeyboardInterrupt``
    的对象让渲染 helper 自己抛出去，Gateway 的异常分支再次失效，原文随之逃逸。
    """

    def __str__(self) -> str:
        raise KeyboardInterrupt(f"str failed: password={CANARY}")


class _SystemExitStrError(RuntimeError):
    def __str__(self) -> str:
        raise SystemExit(f"str failed: password={CANARY}")


class _NonStrReturnError(RuntimeError):
    def __str__(self) -> str:
        return 42  # type: ignore[return-value]


@pytest.mark.parametrize(
    "exc",
    [
        _RaisingStrError(),
        _RaisingBothError(),
        _NonStrReturnError(),
        _BaseExceptionStrError(),
        _SystemExitStrError(),
    ],
    ids=[
        "raising_str",
        "raising_str_and_repr",
        "str_returns_non_str",
        "raising_keyboard_interrupt",
        "raising_system_exit",
    ],
)
def test_never_raises(exc: BaseException) -> None:
    """渲染失败必须降级，不得把失败本身抛给调用方。

    调用点是 Gateway 的 ``except`` 分支——在那里再抛异常，整条"adapter 异常必被
    结构化吸收"的承诺当场失效。
    """
    text = safe_exception_text(exc)
    assert isinstance(text, str)
    assert CANARY not in text


def test_redacts_secrets_in_a_renderable_exception() -> None:
    """正例：能正常渲染时也必须脱敏。"""
    text = safe_exception_text(RuntimeError(f"connect failed: password={CANARY}"))
    assert CANARY not in text
    assert REDACTED in text or "***" in text


def test_keeps_the_exception_type_for_diagnosis() -> None:
    """反例配对：脱敏不等于把诊断信息也抹掉。

    类型名是类的身份、不是数据，必须保留，否则这个函数会退化成一个常量。
    """
    assert "ValueError" in safe_exception_text(ValueError("boom"))


def test_falls_back_when_even_the_type_name_fails() -> None:
    """元类可以让属性访问抛异常——类型名同样不假定为安全。"""

    class _HostileMeta(type):
        @property
        def __name__(cls) -> str:
            raise RuntimeError("name failed")

    class _HostileTypeError(RuntimeError, metaclass=_HostileMeta):
        pass

    text = safe_exception_text(_HostileTypeError("boom"))
    assert isinstance(text, str)


def test_redact_also_survives_baseexception_from_hostile_objects() -> None:
    """同一个洞在 ``redact`` 的映射/序列迭代分支上也存在。

    日志边界与异常边界共用同一条不变量：被记录对象自身出错，绝不能变成调用方
    的异常。
    """
    from xiaowei_agent.redaction import redact

    class _HostileMapping(dict):  # type: ignore[type-arg]
        def items(self):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt(f"password={CANARY}")

    class _HostileSequence(list):  # type: ignore[type-arg]
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt(f"password={CANARY}")

    for hostile in (_HostileMapping(), _HostileSequence()):
        rendered = repr(redact(hostile))
        assert CANARY not in rendered
