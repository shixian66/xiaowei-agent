"""全局 fixture。

不定义自定义的 socket 放行 fixture；未来需要放行时使用 pytest-socket
官方提供的 ``socket_enabled``。
"""

import os
from collections.abc import Iterator

import pytest

_PREFIX = "XIAOWEI_"


@pytest.fixture(autouse=True)
def clean_xiaowei_env() -> Iterator[None]:
    """每个用例前清除全部 XIAOWEI_* 变量，结束后原样恢复。"""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith(_PREFIX)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.upper().startswith(_PREFIX)]:
            del os.environ[k]
        os.environ.update(saved)
