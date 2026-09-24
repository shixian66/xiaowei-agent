"""W4a 三域配置的契约：固定路径、独立代次、Secret 投影与旧运行时引用退场。

这些用例守的是**形状**：三个域各自一份固定文件、各自一个 generation；旧的
``IntegrationConfig`` 只剩迁移输入这一个角色，运行时模块不得再引用它。
"""

import ast
import json
import os
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AiConfig,
    ConfigDomain,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
)
from xiaowei_agent.interfaces.integration_config_file import (
    DEFAULT_AI_CONFIG_PATH,
    DEFAULT_FEISHU_CONFIG_PATH,
    DEFAULT_RESOURCES_CONFIG_PATH,
    read_ai_config,
    read_feishu_config,
    write_ai_config,
    write_feishu_config,
)

_SRC: Final = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_FAKE_KEY: Final = "AIza" + "-w4-not-a-real-key"
_FAKE_SECRET: Final = "app" + "-w4-secret-placeholder"


def test_config_domain_is_the_closed_three_member_set() -> None:
    assert [member.value for member in ConfigDomain] == ["ai", "feishu", "resources"]


def test_each_domain_has_one_fixed_container_path() -> None:
    assert DEFAULT_AI_CONFIG_PATH == "/run/xiaowei-config/ai/config.json"
    assert DEFAULT_FEISHU_CONFIG_PATH == "/run/xiaowei-config/feishu/config.json"
    assert DEFAULT_RESOURCES_CONFIG_PATH == "/run/xiaowei-config/resources/config.json"
    # 三个路径不共享目录：共享目录就意味着一个只读挂载能看见兄弟域。
    directories = {
        os.path.dirname(path)
        for path in (
            DEFAULT_AI_CONFIG_PATH,
            DEFAULT_FEISHU_CONFIG_PATH,
            DEFAULT_RESOURCES_CONFIG_PATH,
        )
    }
    assert len(directories) == 3


def test_domain_documents_carry_only_their_own_section() -> None:
    assert set(AiConfig.model_fields) == {"generation", "gemini"}
    assert set(FeishuConfig.model_fields) == {"generation", "feishu"}


@pytest.mark.parametrize("generation", [0, -1, True, "1", 1.0])
def test_domain_generation_is_a_strict_positive_integer(generation: object) -> None:
    with pytest.raises(ValidationError):
        AiConfig(generation=generation, gemini=GeminiIntegration())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        FeishuConfig(generation=generation, feishu=FeishuIntegration())  # type: ignore[arg-type]


def test_domain_documents_reject_the_other_domain_and_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        AiConfig.model_validate(
            {"generation": 1, "gemini": {"enabled": False}, "feishu": {"enabled": False}}
        )
    with pytest.raises(ValidationError):
        FeishuConfig.model_validate(
            {"generation": 1, "feishu": {"enabled": False}, "gemini": {"enabled": False}}
        )


def test_domain_documents_never_project_secrets() -> None:
    ai = AiConfig(generation=2, gemini=GeminiIntegration(enabled=True, api_key=_FAKE_KEY))
    feishu = FeishuConfig(
        generation=3,
        feishu=FeishuIntegration(enabled=True, app_id="cli_w4", app_secret=_FAKE_SECRET),
    )
    for rendered in (
        repr(ai),
        str(ai),
        ai.model_dump_json(),
        repr(ai.model_dump()),
        repr(feishu),
        str(feishu),
        feishu.model_dump_json(),
        repr(feishu.model_dump()),
    ):
        assert _FAKE_KEY not in rendered
        assert _FAKE_SECRET not in rendered
    assert "cli_w4" in repr(feishu)


def test_ai_and_feishu_round_trip_through_their_own_fixed_files(tmp_path: Path) -> None:
    ai_path = tmp_path / "ai" / "config.json"
    feishu_path = tmp_path / "feishu" / "config.json"
    ai_path.parent.mkdir()
    feishu_path.parent.mkdir()
    write_ai_config(
        str(ai_path),
        AiConfig(generation=4, gemini=GeminiIntegration(enabled=True, api_key=_FAKE_KEY)),
    )
    write_feishu_config(
        str(feishu_path),
        FeishuConfig(
            generation=9,
            feishu=FeishuIntegration(enabled=True, app_id="cli_w4", app_secret=_FAKE_SECRET),
        ),
    )
    ai = read_ai_config(str(ai_path))
    feishu = read_feishu_config(str(feishu_path))
    assert (ai.generation, ai.gemini.secret_value()) == (4, _FAKE_KEY)
    assert (feishu.generation, feishu.feishu.secret_value()) == (9, _FAKE_SECRET)
    # 每个文件只装自己的那一节。
    assert set(json.loads(ai_path.read_text(encoding="utf-8"))) == {"generation", "gemini"}
    assert set(json.loads(feishu_path.read_text(encoding="utf-8"))) == {
        "generation",
        "feishu",
    }


def test_writing_ai_never_touches_the_feishu_file_or_generation(tmp_path: Path) -> None:
    """正常对照：AI 保存推进 AI 的代次，飞书文件的 inode、mtime 与代次都不动。"""
    ai_path = tmp_path / "ai" / "config.json"
    feishu_path = tmp_path / "feishu" / "config.json"
    ai_path.parent.mkdir()
    feishu_path.parent.mkdir()
    write_feishu_config(
        str(feishu_path),
        FeishuConfig(
            generation=7,
            feishu=FeishuIntegration(enabled=True, app_id="cli_w4", app_secret=_FAKE_SECRET),
        ),
    )
    before = feishu_path.stat()
    for generation in (1, 2, 3):
        write_ai_config(
            str(ai_path), AiConfig(generation=generation, gemini=GeminiIntegration())
        )
    after = feishu_path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert read_feishu_config(str(feishu_path)).generation == 7
    assert read_ai_config(str(ai_path)).generation == 3


def test_ai_reader_rejects_a_feishu_document_and_vice_versa(tmp_path: Path) -> None:
    """把一个域的文件放到另一个域的路径上必须失败，而不是半读。"""
    from xiaowei_agent.interfaces.integration_config_file import IntegrationConfigError

    ai_path = tmp_path / "config.json"
    write_feishu_config(
        str(ai_path), FeishuConfig(generation=1, feishu=FeishuIntegration())
    )
    with pytest.raises(IntegrationConfigError):
        read_ai_config(str(ai_path))
    write_ai_config(str(ai_path), AiConfig(generation=1, gemini=GeminiIntegration()))
    with pytest.raises(IntegrationConfigError):
        read_feishu_config(str(ai_path))


# --------------------------------------------------------------------------
# 旧运行时引用必须消失
# --------------------------------------------------------------------------

_LEGACY_ALLOWED_MODULES: Final = frozenset(
    {
        "contracts/__init__.py",
        "contracts/integration_config.py",
        "interfaces/integration_config_file.py",
        "interfaces/integration_config_migrate.py",
    }
)
"""只有契约定义处、文件边界与一次性迁移器可以提到旧组合契约。"""

_LEGACY_NAMES: Final = frozenset(
    {
        "IntegrationConfig",
        "read_integration_config",
        "write_integration_config",
        "read_legacy_integration_config",
        "DEFAULT_INTEGRATION_CONFIG_PATH",
    }
)


def _referenced_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])
    return names


def test_runtime_modules_no_longer_reference_the_legacy_combined_config() -> None:
    offenders = sorted(
        (path.relative_to(_SRC).as_posix(), sorted(_referenced_names(path) & _LEGACY_NAMES))
        for path in _SRC.rglob("*.py")
        if path.relative_to(_SRC).as_posix() not in _LEGACY_ALLOWED_MODULES
        and _referenced_names(path) & _LEGACY_NAMES
    )
    assert not offenders, f"运行时模块仍引用旧组合配置：{offenders}"


def test_only_the_file_boundary_and_migrator_read_the_legacy_document() -> None:
    readers = sorted(
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if "read_legacy_integration_config" in _referenced_names(path)
    )
    assert readers == [
        "interfaces/integration_config_file.py",
        "interfaces/integration_config_migrate.py",
    ]
