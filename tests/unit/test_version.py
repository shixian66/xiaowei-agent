"""版本单一真源：pyproject.toml 的 [project].version 是唯一来源。"""

import tomllib
from pathlib import Path

import xiaowei_agent


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert xiaowei_agent.__version__ == pyproject["project"]["version"]


def test_version_is_non_empty() -> None:
    assert xiaowei_agent.__version__.strip()
