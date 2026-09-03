"""integration gate 不可静默死亡。

DSN 已设置却整组跳过，是 CI 里最危险的一种绿：DSN 拼错、service 没起来、迁移失败
全都长成这个样子，而 M4 的全部退出标准都建立在那些用例真的跑过之上。

判定写成纯函数，因此可以在**不真的制造一次跳过**的前提下测试它自己——否则这条
护栏只能靠"某次 CI 恰好出问题"来验证。
"""

import os

from tests.integration.conftest import unexpected_integration_skips

_INSIDE = f"tests{os.sep}integration{os.sep}test_lease_fencing_postgres.py::test_x"
_OUTSIDE = f"tests{os.sep}security{os.sep}test_no_network.py::test_y"


def test_no_dsn_means_skips_are_expected() -> None:
    """没有 DSN 时跳过是正常的，不能报错——否则本机开发一律红。"""
    assert unexpected_integration_skips([_INSIDE], dsn=None) == []


def test_dsn_set_makes_an_integration_skip_an_error() -> None:
    assert unexpected_integration_skips([_INSIDE], dsn="postgresql://x/y") == [_INSIDE]


def test_skips_outside_the_integration_directory_are_ignored() -> None:
    """目录外的跳过与这条 gate 无关，不能误报——误报会让人把整条 gate 关掉。"""
    assert unexpected_integration_skips([_OUTSIDE], dsn="postgresql://x/y") == []


def test_the_check_reports_every_offender_not_just_the_first() -> None:
    other = _INSIDE.replace("test_x", "test_z")
    assert unexpected_integration_skips(
        [_INSIDE, _OUTSIDE, other], dsn="postgresql://x/y"
    ) == [_INSIDE, other]
