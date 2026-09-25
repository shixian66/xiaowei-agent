"""fake 随包发布（DEVELOPMENT_PLAN §7 M2 明文要求），因此必须可被审计地隔离。

代价是"fake 可能被接进真实路径"，用三条断言抵消：每个 fake 模块自我标识、不从包
入口导出、且没有任何生产模块导入它。
"""

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_FAKE_MODULES = (
    "tools.fake",
    "tools.starrocks_fake",
    "tools.starrocks_recording",
    "tools.alertmanager_fake",
    "tools.alertmanager_recording",
    "tools.prometheus_fake",
    "tools.prometheus_recording",
    "tools.asset_inventory_fake",
    "tools.asset_inventory_recording",
    "persistence.fake",
    "runners.fake",
)


@pytest.mark.parametrize("name", _FAKE_MODULES)
def test_every_fake_module_declares_is_fake(name: str) -> None:
    module = importlib.import_module(f"xiaowei_agent.{name}")
    assert getattr(module, "IS_FAKE", False) is True


def _type_checking_line_ranges(tree: ast.Module) -> list[tuple[int, int]]:
    """``if TYPE_CHECKING:`` 分支的行范围。

    这类 import 在运行时**不发生**，因此不可能把 fake 接进真实执行路径——而这正是
    本文件要防的事。把它们算作违规会逼人放弃类型锚点（``_conformance.py``），
    反而削弱静态检查。

    用行范围而不是改写 AST：``getattr(node, "body")`` 在 f-string 等节点上取到的
    并不是语句列表，按 body 递归会 ``TypeError``。
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_tc and node.body:
            ranges.append((node.body[0].lineno, node.body[-1].end_lineno or node.body[-1].lineno))
    return ranges


def _imports_a_fake(path: Path) -> list[str]:
    """扫 AST 的**运行时** import，而不是原始文本。

    文本扫描会被 docstring 里"不从本入口导出 fake"这句话本身触发，断言的就不再
    是代码行为。
    """
    hits: list[str] = []
    fake_names = {f"xiaowei_agent.{name}" for name in _FAKE_MODULES}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded = _type_checking_line_ranges(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and any(
            lo <= node.lineno <= hi for lo, hi in guarded
        ):
            continue
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if module in fake_names:
            hits.append(module or "")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported = f"{node.module}.{alias.name}"
                if imported in fake_names:
                    hits.append(imported)
    return hits


@pytest.mark.parametrize("name", _FAKE_MODULES)
def test_fake_is_not_exported_from_its_package_init(name: str) -> None:
    package, _, _ = name.partition(".")
    init = _SRC / package / "__init__.py"
    assert not _imports_a_fake(init), f"{package}/__init__.py 不得导出 fake"


def test_the_detector_catches_both_import_spellings(tmp_path: Path) -> None:
    """检测器自身必须先被证明有效。"""
    a = tmp_path / "a.py"
    a.write_text("from xiaowei_agent.tools import fake" + chr(10), encoding="utf-8")
    assert _imports_a_fake(a)

    b = tmp_path / "b.py"
    b.write_text(
        "from xiaowei_agent.tools.fake import RecordingToolAdapter" + chr(10),
        encoding="utf-8",
    )
    assert _imports_a_fake(b)

    c = tmp_path / "c.py"
    c.write_text(
        '"""docstring 提到 fake 但不 import。"""' + chr(10)
        + "from xiaowei_agent import contracts" + chr(10),
        encoding="utf-8",
    )
    assert not _imports_a_fake(c)

    # TYPE_CHECKING 块内的 import 运行时不发生，不构成"接进真实路径"。
    d = tmp_path / "d.py"
    d.write_text(
        "from typing import TYPE_CHECKING" + chr(10)
        + "if TYPE_CHECKING:" + chr(10)
        + "    from xiaowei_agent.tools.fake import RecordingToolAdapter" + chr(10),
        encoding="utf-8",
    )
    assert not _imports_a_fake(d)

    # 但同一个文件在运行时 import 就必须被抓住。
    e = tmp_path / "e.py"
    e.write_text(
        "from typing import TYPE_CHECKING" + chr(10)
        + "from xiaowei_agent.tools.fake import RecordingToolAdapter" + chr(10)
        + "if TYPE_CHECKING:" + chr(10)
        + "    pass" + chr(10),
        encoding="utf-8",
    )
    assert _imports_a_fake(e)

    f = tmp_path / "f.py"
    f.write_text(
        "from xiaowei_agent.tools import starrocks_fake" + chr(10),
        encoding="utf-8",
    )
    assert _imports_a_fake(f) == ["xiaowei_agent.tools.starrocks_fake"]

    g = tmp_path / "g.py"
    g.write_text(
        "from xiaowei_agent.tools.starrocks_recording import default_recording"
        + chr(10),
        encoding="utf-8",
    )
    assert _imports_a_fake(g) == ["xiaowei_agent.tools.starrocks_recording"]


def test_no_production_module_imports_a_fake() -> None:
    fake_paths = {_SRC / f"{name.replace('.', '/')}.py" for name in _FAKE_MODULES}
    allowed = {_SRC / "interfaces/local_stack.py"}
    offenders = [
        (str(path.relative_to(_SRC)), hit)
        for path in _SRC.rglob("*.py")
        if path not in fake_paths | allowed
        for hit in _imports_a_fake(path)
    ]
    assert not offenders, f"生产模块不得导入 fake: {offenders}"


def test_src_never_imports_test_recordings() -> None:
    offenders = [
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if "tests.fakes.recordings" in path.read_text(encoding="utf-8")
    ]
    assert not offenders


def test_only_local_stack_imports_a_fake_at_runtime() -> None:
    fake_paths = {_SRC / f"{name.replace('.', '/')}.py" for name in _FAKE_MODULES}
    importers = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if path not in fake_paths and _imports_a_fake(path)
    }
    assert importers == {"interfaces/local_stack.py"}


# --- W5 release：五个进程装配后不得加载任何 fake/recording 模块 -----------------

_RELEASE_BUILD_SCRIPT = r'''
import asyncio
import json
import sys

from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces import api, feishu_listener, feishu_worker, web_app, worker
from xiaowei_agent.interfaces import local_stack
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

PROFILE = __PROFILE__


class _Result:
    def mappings(self):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _Connection:
    async def execute(self, *_, **__):
        return _Result()

    async def scalar(self, *_, **__):
        return "inserted"


class _Engine:
    async def dispose(self):
        return None

    def begin(self):
        connection = _Connection()

        class _Transaction:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *_):
                return False

        return _Transaction()

    connect = begin


class _Messages:
    async def send_to_chat(self, **_):
        return "m"

    async def send_to_user(self, **_):
        return "m"

    async def update_card(self, **_):
        return None


class _Transport:
    def run_forever(self, *, on_event):
        raise AssertionError("must not run")


local_stack.create_database_engine = lambda _settings: _Engine()


def _settings(**updates):
    base = {"environment_id": "dev"}
    if PROFILE == "release":
        base |= {"runtime_profile": "release", "starrocks_adapter_mode": "disabled"}
    return Settings.model_validate(base | updates)


def _view_snapshot(runtime):
    return vars(runtime)["_conversation_snapshot"].snapshot_id


async def main():
    snapshots = {}
    credentials = ProviderCredentials(
        feishu_app_id="cli_release", feishu_app_secret="release-" + "fixture"
    )
    stack = await local_stack.build_postgres_task_view_stack(settings=_settings())
    snapshots["internal-api"] = _view_snapshot(stack.runtime)
    stack = await local_stack.build_postgres_web_stack(
        settings=_settings(web_app_enabled=True, web_public_origin="https://ops.example.test")
    )
    snapshots["web-app"] = _view_snapshot(stack.runtime)
    stack = await local_stack.build_postgres_feishu_listener_stack(
        settings=_settings(
            feishu_listener_enabled=True,
            feishu_tenant_key="tenant-test",
            feishu_bot_open_id="bot-open-id",
        ),
        transport=_Transport(),
        message_port=_Messages(),
        credentials=credentials,
    )
    snapshots["feishu-listener"] = _view_snapshot(stack.runtime)
    stack = await local_stack.build_postgres_channel_worker_stack(
        settings=_settings(
            channel_worker_enabled=True, web_public_origin="https://ops.example.test"
        ),
        message_port=_Messages(),
    )
    snapshots["channel-worker"] = _view_snapshot(stack.runtime)
    stack = await local_stack.build_postgres_local_stack(
        settings=_settings(), credentials=ProviderCredentials()
    )
    snapshots["worker"] = _view_snapshot(stack.runtime._task_views)
    loaded = sorted(
        name.removeprefix("xiaowei_agent.")
        for name in sys.modules
        if name.startswith("xiaowei_agent.")
    )
    print(json.dumps({"loaded": loaded, "snapshots": snapshots}))


asyncio.run(main())
assert (api, feishu_listener, feishu_worker, web_app, worker)
'''


def _build_five_processes(profile: str) -> dict[str, object]:
    import json
    import subprocess
    import sys

    completed = subprocess.run(  # noqa: S603 -- 当前解释器与程序均由测试控制
        [
            sys.executable,
            "-c",
            _RELEASE_BUILD_SCRIPT.replace("__PROFILE__", repr(profile)),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.splitlines()[-1])


def test_release_builders_load_no_fake_module_and_answer_from_the_empty_snapshot() -> None:
    from xiaowei_agent.capabilities.registry import PROVIDER_OFF_SNAPSHOT_ID

    result = _build_five_processes("release")

    assert set(result["loaded"]) & set(_FAKE_MODULES) == set()  # type: ignore[arg-type]
    # 旧静态身份文档只属于一次性迁移命令；五个长期进程都不加载它。
    assert "interfaces.legacy_identity_migration" not in result["loaded"]  # type: ignore[operator]
    assert result["snapshots"] == {
        name: PROVIDER_OFF_SNAPSHOT_ID
        for name in (
            "internal-api",
            "web-app",
            "feishu-listener",
            "channel-worker",
            "worker",
        )
    }


def test_offline_builders_still_load_recordings_so_the_release_probe_is_not_vacuous() -> None:
    """反向对照：同一脚本在 offline 形态下必须能观测到 recording 模块。"""
    from xiaowei_agent.capabilities.registry import SNAPSHOT_ID

    result = _build_five_processes("offline_recording")

    assert {"tools.starrocks_fake", "tools.prometheus_recording"} <= set(
        result["loaded"]  # type: ignore[arg-type]
    )
    assert set(result["snapshots"].values()) == {SNAPSHOT_ID}  # type: ignore[union-attr]
