"""生产依赖面与类型检查严格度是同一条边界。

M4 引入 SQLAlchemy / Alembic / asyncpg，M5 引入 FastAPI / uvicorn，M6b 引入
PyMySQL 与对应 typeshed stub，M7 引入官方飞书 SDK ``lark-oapi``，RI3 PR 3B
引入官方模型 SDK ``google-genai``，并将 SDK transport 泄漏类型 ``httpx`` 提升为
生产直接依赖以做精确错误分类。
两件事必须被机制钉住，而不是靠计划里的一句话：

1. **依赖面**。M5 当时只新增 HTTP gateway 所需的 FastAPI / uvicorn，以及开发侧的
   PyYAML；当前仍不引入 Redis、队列、Worker 框架或 LangGraph。
   用集合相等表达比用禁用清单强：禁用清单挡不住清单外的新依赖。

2. **``asyncpg`` 不得被 ``src/`` 直接 import**。T0 实测：``asyncpg`` 0.30.0 **不带
   ``py.typed``**，直接 import 会让 ``mypy --strict`` 报 ``import-untyped``。M4 计划
   §5.3 给的退路是"对单个模块加 ``ignore_missing_imports``"，但那条退路不必走：
   驱动只经 ``postgresql+asyncpg://`` 的 DSN 方言字符串被 SQLAlchemy 内部加载，
   业务代码从不需要它的符号。**不 import 就不需要任何放宽**，全局 strict 完好。
"""

import ast
import tomllib
from importlib.metadata import metadata
from importlib.util import find_spec
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"

# ``[project].dependencies`` 的包名集合。新增任何一项都必须先改这里，
# 从而必须在 review 里被看见。
_EXPECTED_RUNTIME_DEPENDENCIES = frozenset(
    {
        "pydantic",
        "sqlglot",
        "sqlalchemy",
        "alembic",
        "asyncpg",
        "fastapi",
        "uvicorn",
        "pymysql",
        "lark-oapi",
        "google-genai",
        "httpx",
    }
)

_EXPECTED_DEV_DEPENDENCIES = frozenset(
    {
        "pytest",
        "pytest-asyncio",
        "pytest-socket",
        "ruff",
        "mypy",
        "pip-audit",
        "hatchling",
        "pyyaml",
        "types-pymysql",
    }
)


def _pyproject() -> dict[str, object]:
    data: dict[str, object] = tomllib.loads(
        (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return data


def _requirement_names(specs: list[str]) -> set[str]:
    """``"sqlalchemy[asyncio]>=2.0.44,<2.1"`` → ``"sqlalchemy"``。"""
    names: set[str] = set()
    for spec in specs:
        head = spec.split(";", 1)[0].strip()
        for separator in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(separator, 1)[0]
        names.add(head.strip().lower().replace("_", "-"))
    return names


def _internal_module_imports(path: Path) -> set[str]:
    """文件中出现的所有顶层第三方/标准库模块名。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_runtime_dependency_set_is_exactly_the_approved_set() -> None:
    """依赖面用集合相等钉死，不用禁用清单。

    禁用清单只挡得住已经想到的那些；集合相等连"想不到的"一起挡住。
    """
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = project["dependencies"]
    assert isinstance(declared, list)
    assert _requirement_names(declared) == set(_EXPECTED_RUNTIME_DEPENDENCIES)


def test_dev_dependency_set_is_exactly_the_approved_tools() -> None:
    """M5+ 的基础设施依赖也不得从 ``dev`` 这条侧门进来。

    ``dev`` 不是"随便放"的口袋：一个只在 dev 出现的 FastAPI 同样会让人开始写
    interfaces 层，而它的准入门在 M5。
    """
    optional = _pyproject()["project"]
    assert isinstance(optional, dict)
    dev = optional["optional-dependencies"]
    assert isinstance(dev, dict)
    dev_specs = dev["dev"]
    assert isinstance(dev_specs, list)
    assert _requirement_names(dev_specs) == set(_EXPECTED_DEV_DEPENDENCIES)
    assert not ({"fastapi", "uvicorn"} & _requirement_names(dev_specs))


@pytest.mark.parametrize("package", ["sqlalchemy", "alembic"])
def test_typed_dependency_ships_py_typed(package: str) -> None:
    """SQLAlchemy 与 Alembic 自带 ``py.typed``，因此 strict 下无需任何放宽。

    这条转红意味着上游撤回了类型标注，届时必须重新评估 §5.3.1 的结论，
    而不是顺手加 ``ignore_missing_imports``。
    """
    spec = find_spec(package)
    assert spec is not None and spec.origin is not None
    assert (Path(spec.origin).parent / "py.typed").is_file()


def test_asyncpg_still_lacks_py_typed() -> None:
    """反向 tripwire：``asyncpg`` 目前**没有** ``py.typed``。

    这条断言故意钉住上游的现状。一旦 asyncpg 发布 ``py.typed``，它会转红——那正是
    重新决定"是否允许 ``src/`` 直接 import asyncpg"的时刻。没有这条，下面那条禁令
    会变成一条无人知道何时可以撤销的永久规则。
    """
    spec = find_spec("asyncpg")
    assert spec is not None and spec.origin is not None
    marker = Path(spec.origin).parent / "py.typed"
    assert not marker.is_file(), (
        "asyncpg 现已自带 py.typed：可重新评估 test_src_never_imports_asyncpg_directly 的禁令"
    )


def test_lark_oapi_is_exactly_pinned_and_still_lacks_py_typed() -> None:
    """M7 的单一 SDK seam 依赖锁版且不得假装上游已提供类型声明。"""
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = project["dependencies"]
    assert isinstance(declared, list)
    assert "lark-oapi==1.7.3" in declared

    spec = find_spec("lark_oapi")
    assert spec is not None and spec.origin is not None
    assert not (Path(spec.origin).parent / "py.typed").is_file()


def test_google_genai_exact_wheel_license_and_typing_marker_are_locked() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    declared = project["dependencies"]
    assert isinstance(declared, list)
    assert "google-genai==2.23.0" in declared
    assert "httpx>=0.28,<1" in declared

    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    assert isinstance(packages, list)
    matches = [item for item in packages if item.get("name") == "google-genai"]
    assert len(matches) == 1
    package = matches[0]
    assert package["version"] == "2.23.0"
    assert {
        (item["url"].rsplit("/", 1)[-1], item["hash"])
        for item in package["wheels"]
    } == {
        (
            "google_genai-2.23.0-py3-none-any.whl",
            "sha256:1e63211d44d188b8069c2b354d92b9bde25c1e821513fdbe1948b7c0d9f6b922",
        )
    }

    assert metadata("google-genai")["License-Expression"] == "Apache-2.0"
    spec = find_spec("google.genai")
    assert spec is not None and spec.origin is not None
    assert (Path(spec.origin).parent / "py.typed").is_file()


def test_lark_oapi_wheel_digest_is_locked_to_the_reviewed_artifact() -> None:
    """锁文件必须引用 PR 4 实测过的 1.7.3 universal wheel。"""
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    assert isinstance(packages, list)
    matches = [item for item in packages if item.get("name") == "lark-oapi"]
    assert len(matches) == 1
    package = matches[0]
    assert package["version"] == "1.7.3"
    wheels = package["wheels"]
    assert isinstance(wheels, list)
    assert {
        (item["url"].rsplit("/", 1)[-1], item["hash"])
        for item in wheels
    } == {
        (
            "lark_oapi-1.7.3-py3-none-any.whl",
            "sha256:c91f00087b7977dc9059ab492e8fe435e1a873863dca1d4e660d2be5b801e4cd",
        )
    }


def _class_method_kinds(path: Path, class_name: str) -> dict[str, type[ast.AST]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert len(classes) == 1
    return {
        node.name: type(node)
        for node in classes[0].body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _class_method_node(
    path: Path, class_name: str, method_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = [
        node
        for candidate in tree.body
        if isinstance(candidate, ast.ClassDef) and candidate.name == class_name
        for node in candidate.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == method_name
    ]
    assert len(methods) == 1
    return methods[0]


def _attribute_string_assignments(path: Path, attribute: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and any(
            isinstance(target, ast.Attribute) and target.attr == attribute
            for target in node.targets
        )
    }


def _module_string_assignment(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert len(values) == 1
    return values[0]


def test_lark_oapi_reviewed_license_and_async_surface_still_match() -> None:
    """不用 import SDK（其 ws import 会建事件循环），直接核对锁定 wheel 源码。"""
    assert metadata("lark-oapi")["License"] == "MIT"
    spec = find_spec("lark_oapi")
    assert spec is not None and spec.origin is not None
    root = Path(spec.origin).parent

    messages = _class_method_kinds(
        root / "api/im/v1/resource/message.py", "Message"
    )
    members = _class_method_kinds(
        root / "api/im/v1/resource/chat_members.py", "ChatMembers"
    )
    websocket = _class_method_kinds(root / "ws/client.py", "Client")
    dispatcher = _class_method_kinds(
        root / "event/dispatcher_handler.py", "EventDispatcherHandlerBuilder"
    )

    assert all(
        messages[name] is ast.AsyncFunctionDef for name in ("acreate", "apatch", "aget")
    )
    assert members["aget"] is ast.AsyncFunctionDef
    assert websocket["start"] is ast.FunctionDef
    assert "register_p2_im_message_receive_v1" in dispatcher


def test_lark_oapi_provider_domain_and_relative_uri_seams_still_match() -> None:
    """显式 project origin 必须仍控制 SDK 的 REST 与 WS 首跳。"""
    spec = find_spec("lark_oapi")
    assert spec is not None and spec.origin is not None
    root = Path(spec.origin).parent

    assert "domain" in _class_method_kinds(root / "client.py", "ClientBuilder")
    ws_init = _class_method_node(root / "ws/client.py", "Client", "__init__")
    ws_parameters = {
        argument.arg
        for argument in (
            *ws_init.args.posonlyargs,
            *ws_init.args.args,
            *ws_init.args.kwonlyargs,
        )
    }
    assert "domain" in ws_parameters

    expected_uris = {
        root / "core/token/create_self_tenant_token_request.py": (
            "/open-apis/auth/v3/tenant_access_token/internal"
        ),
        root / "api/im/v1/model/get_chat_members_request.py": (
            "/open-apis/im/v1/chats/:chat_id/members"
        ),
        root / "api/im/v1/model/create_message_request.py": (
            "/open-apis/im/v1/messages"
        ),
        root / "api/im/v1/model/patch_message_request.py": (
            "/open-apis/im/v1/messages/:message_id"
        ),
    }
    for path, expected in expected_uris.items():
        assert _attribute_string_assignments(path, "uri") == {expected}
        assert expected.startswith("/") and "://" not in expected
    websocket_uri = _module_string_assignment(root / "ws/const.py", "GEN_ENDPOINT_URI")
    assert websocket_uri == "/callback/ws/endpoint"
    assert "://" not in websocket_uri


def test_lark_oapi_message_builder_surface_still_matches_the_typed_seam() -> None:
    """离线读 wheel 源码核对卡片 create/patch builder，不 import 有副作用的 SDK。"""
    spec = find_spec("lark_oapi")
    assert spec is not None and spec.origin is not None
    models = Path(spec.origin).parent / "api/im/v1/model"
    required = {
        ("create_message_request.py", "CreateMessageRequestBuilder"): {
            "receive_id_type",
            "request_body",
            "build",
        },
        ("create_message_request_body.py", "CreateMessageRequestBodyBuilder"): {
            "receive_id",
            "msg_type",
            "content",
            "uuid",
            "build",
        },
        ("patch_message_request.py", "PatchMessageRequestBuilder"): {
            "message_id",
            "request_body",
            "build",
        },
        ("patch_message_request_body.py", "PatchMessageRequestBodyBuilder"): {
            "content",
            "build",
        },
    }
    for (filename, class_name), methods in required.items():
        assert methods <= set(_class_method_kinds(models / filename, class_name))


def test_src_never_imports_asyncpg_directly() -> None:
    """驱动只经 DSN 方言字符串加载，业务代码不得 import 它的符号。

    直接 import 会在 ``mypy --strict`` 下产生 ``import-untyped``，唯一的消解办法是
    放宽类型检查。禁止 import 让这个取舍根本不出现。
    """
    offenders = [
        str(path.relative_to(_ROOT))
        for path in _SRC.rglob("*.py")
        if "asyncpg" in _internal_module_imports(path)
    ]
    assert not offenders, f"src/ 不得直接 import asyncpg（无 py.typed）：{offenders}"


def test_mypy_stays_globally_strict() -> None:
    """全局 strict 不得被放宽，也不得出现全局 ``ignore_missing_imports``。

    §5.3 只允许**逐模块** override。全局开关会让所有第三方缺失存根一起静默，
    包括将来引入的、我们还没看过的那些。
    """
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    mypy_config = tool["mypy"]
    assert isinstance(mypy_config, dict)
    assert mypy_config["strict"] is True
    assert "ignore_missing_imports" not in mypy_config
    assert "follow_imports" not in mypy_config
