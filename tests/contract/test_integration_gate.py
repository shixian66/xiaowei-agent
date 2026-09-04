"""integration gate 不可静默死亡。

DSN 已设置却整组跳过，是 CI 里最危险的一种绿：DSN 拼错、service 没起来、迁移失败
全都长成这个样子，而 M4 的全部退出标准都建立在那些用例真的跑过之上。

判定写成纯函数，因此可以在**不真的制造一次跳过**的前提下测试它自己——否则这条
护栏只能靠"某次 CI 恰好出问题"来验证。
"""

import os
import re
from pathlib import Path

from sqlalchemy.engine import make_url
from tests.integration.conftest import (
    DSN_ENV_VAR,
    unexpected_integration_skips,
)

_WORKFLOW_TEXT = (
    Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
).read_text(encoding="utf-8")

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


# --- gate 的触发条件本身也要承重 -----------------------------------------------
#
# 上面四条证明的是"DSN 已设置时跳过会被抓到"。但 gate 的**触发条件**是
# ``dsn is not None``，而 dsn 来自 ``DSN_ENV_VAR`` 这个名字——它在 ``ci.yml`` 里
# 还有第二份独立拷贝。改任一侧（重命名 conftest 里的常量、或改掉工作流里的 env 名），
# CI 就读不到 DSN：104 条 integration 用例全部跳过，``unexpected_integration_skips``
# 因 ``dsn`` 为 ``None`` 老老实实返回空，**job 全绿而一条 integration 用例都没跑**。
#
# ``ci.yml`` 的整文件 SHA 门槛挡不住这个方向——被改的是 conftest，工作流一个字节没动。
# ``test_workflow_policy.py`` 的 env 白名单也挡不住：白名单只能挡"多出来的东西"，
# 挡不住改名或删除。因此这里把两侧钉在一起。


def _declared_env_names(text: str) -> set[str]:
    """工作流里所有 ``NAME: value`` 形态的 env 键。"""
    return set(re.findall(r"(?m)^\s+([A-Z][A-Z0-9_]*):\s", text))


def test_the_workflow_declares_the_exact_env_var_the_gate_reads() -> None:
    """两侧同名，否则 gate 在 CI 里根本不会被触发。"""
    assert DSN_ENV_VAR in _declared_env_names(_WORKFLOW_TEXT), (
        f"ci.yml 没有声明 {DSN_ENV_VAR}：integration 会整组跳过而 job 仍然全绿"
    )


def test_the_env_name_binding_is_discriminating() -> None:
    """反空洞：上一条若换成任何别的名字必须失败，否则它证明不了"同名"。"""
    assert f"{DSN_ENV_VAR}_RENAMED" not in _declared_env_names(_WORKFLOW_TEXT)
    assert "PYTEST_POSTGRES_DSN_TYPO" not in _declared_env_names(_WORKFLOW_TEXT)


def test_the_workflow_dsn_points_at_the_declared_service_port() -> None:
    """DSN 的 host/port 必须就是 service 发布出来的那个端口。

    对不上时的表现是连接被拒（红），不是静默跳过——但那要等 CI 跑到才知道。
    这条把它提前到默认路径上，顺带钉死 host 是回环地址：``allow_hosts`` 只放行
    DSN 解析出的这一个 host，写成别的会同时改变网络放行面。
    """
    match = re.search(rf"(?m)^\s+{DSN_ENV_VAR}:\s*(\S+)$", _WORKFLOW_TEXT)
    assert match, f"ci.yml 里 {DSN_ENV_VAR} 没有取值"
    url = make_url(match.group(1))
    assert url.host == "127.0.0.1", url.host
    assert url.port == 5432, url.port
    assert f"- {url.port}:{url.port}\n" in _WORKFLOW_TEXT, "service 未发布该端口"


def test_the_integration_job_actually_runs_the_suite() -> None:
    """env 对了、端口对了，但没有一步真的跑 pytest，同样是零证据。

    与 ``test_workflow_policy.py::test_the_integration_job_runs_no_extra_command``
    互补：那条管"不多跑"，这条管"确实跑了"。
    """
    assert _WORKFLOW_TEXT.count("- run: python -m pytest -q\n") == 2
