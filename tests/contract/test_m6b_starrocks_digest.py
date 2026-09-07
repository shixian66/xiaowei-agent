"""M6b 带外 digest 工具与 runtime normalizer 的同源契约。"""

import io
import json
from pathlib import Path

import pytest
from scripts.m6b_starrocks_digest import main

from xiaowei_agent.tools.starrocks import preflight_digest


def test_digest_tool_emits_only_the_runtime_digest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = ["GRANT SELECT ON db.tbl TO reader", "GRANT USAGE ON *.* TO reader"]
    stdin = io.StringIO(json.dumps(values))

    assert main(["--stdin", "--order-insensitive"], stdin=stdin) == 0

    captured = capsys.readouterr()
    assert captured.out == preflight_digest(values, order_insensitive=True) + "\n"
    assert captured.err == ""
    assert all(value not in captured.out for value in values)


def test_digest_tool_accepts_one_regular_bounded_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = "CREATE TABLE `db`.`tbl` (`id` BIGINT)"
    source = tmp_path / "approved-discovery.json"
    source.write_text(json.dumps([value]), encoding="utf-8")

    assert main(["--file", str(source)], stdin=io.StringIO("")) == 0

    assert capsys.readouterr().out == preflight_digest(
        (value,), order_insensitive=False
    ) + "\n"


@pytest.mark.parametrize("payload", ["not-json", "{}", "[]", '["ok", 1]'])
def test_digest_tool_rejects_malformed_input_without_echo(
    payload: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--stdin"], stdin=io.StringIO(payload)) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert payload not in captured.err


def test_digest_tool_rejects_symlink_and_oversize_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "target.json"
    target.write_text('["approved"]', encoding="utf-8")
    link = tmp_path / "input.json"
    link.symlink_to(target)
    assert main(["--file", str(link)], stdin=io.StringIO("")) == 2
    assert str(link) not in capsys.readouterr().err

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 1_048_577)
    assert main(["--file", str(oversized)], stdin=io.StringIO("")) == 2
    assert str(oversized) not in capsys.readouterr().err


def test_digest_tool_requires_exactly_one_input_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([], stdin=io.StringIO("[]")) == 2
    assert main(["--stdin", "--file", "unused"], stdin=io.StringIO("[]")) == 2
    assert "unused" not in capsys.readouterr().err
