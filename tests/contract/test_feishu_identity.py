"""静态飞书身份目录：显式 allowlist、标签映射和 scope 拒绝。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from xiaowei_agent.contracts import ChannelPermission, IdentitySource
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityConfigurationError,
    FeishuIdentityNotFoundError,
    StaticFeishuIdentityDirectory,
    load_feishu_identity_directory,
)


def _document(*entries: dict[str, object], **updates: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 1,
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "entries": list(entries),
    }
    return base | updates


def _entry(
    subject_ref: str,
    actor: str,
    *labels: str,
) -> dict[str, object]:
    return {
        "subject_ref": subject_ref,
        "actor": actor,
        "labels": list(labels),
    }


def _load(tmp_path: Path, document: dict[str, object]) -> StaticFeishuIdentityDirectory:
    path = tmp_path / "identities.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_feishu_identity_directory(
        path=str(path),
        tenant_id="dev-local",
        environment_id="dev",
    )


@pytest.mark.parametrize("label", ["operator", "dba", "oncall"])
def test_operational_labels_map_to_view_and_submit(
    tmp_path: Path, label: str
) -> None:
    directory = _load(tmp_path, _document(_entry("subject-alice", "alice", label)))

    principal = directory.resolve(subject_ref="subject-alice")

    assert principal.tenant_id == "dev-local"
    assert principal.environment_id == "dev"
    assert principal.actor == "alice"
    assert principal.source is IdentitySource.FEISHU
    assert principal.permissions == frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    )


@pytest.mark.parametrize("label", ["viewer", "approver"])
def test_readonly_labels_map_only_to_safe_view(tmp_path: Path, label: str) -> None:
    directory = _load(tmp_path, _document(_entry("subject-alice", "alice", label)))

    assert directory.resolve(subject_ref="subject-alice").permissions == frozenset(
        {ChannelPermission.VIEW_SAFE_TASK}
    )


def test_admin_maps_to_the_complete_m7_permission_set(tmp_path: Path) -> None:
    directory = _load(tmp_path, _document(_entry("subject-root", "root", "admin")))

    assert directory.resolve(subject_ref="subject-root").permissions == frozenset(
        ChannelPermission
    )


def test_unknown_subject_is_not_inferred_from_actor_or_label(tmp_path: Path) -> None:
    directory = _load(
        tmp_path,
        _document(_entry("subject-alice", "operations-admin", "operator")),
    )

    with pytest.raises(FeishuIdentityNotFoundError, match="identity not found"):
        directory.resolve(subject_ref="operations-admin")


@pytest.mark.parametrize(
    "document",
    [
        _document(_entry("subject-a", "alice", "unknown-label")),
        _document(
            _entry("subject-a", "alice", "operator"),
            _entry("subject-b", "alice", "viewer"),
        ),
        _document(
            _entry("subject-a", "alice", "operator"),
            _entry("subject-a", "bob", "viewer"),
        ),
        _document(_entry("subject-a", "alice", "operator"), tenant_id="other"),
        _document(_entry("subject-a", "alice", "operator"), environment_id="prod"),
        _document(_entry("subject-a", "alice", "operator"), unexpected="value"),
    ],
    ids=[
        "unknown-label",
        "duplicate-actor",
        "duplicate-subject",
        "cross-tenant",
        "cross-environment",
        "unknown-field",
    ],
)
def test_invalid_identity_documents_fail_closed_without_echoing_values(
    tmp_path: Path, document: dict[str, object]
) -> None:
    raw = json.dumps(document)
    path = tmp_path / "identities.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(FeishuIdentityConfigurationError) as caught:
        load_feishu_identity_directory(
            path=str(path), tenant_id="dev-local", environment_id="dev"
        )

    assert str(caught.value) == "feishu identity configuration invalid"
    assert "subject-a" not in str(caught.value)


def test_missing_or_oversized_identity_file_fails_with_one_safe_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (1_048_576 + 1))

    for path in (missing, oversized):
        with pytest.raises(
            FeishuIdentityConfigurationError,
            match=r"^feishu identity configuration invalid$",
        ):
            load_feishu_identity_directory(
                path=str(path), tenant_id="dev-local", environment_id="dev"
            )


def test_identity_file_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(_document(_entry("subject-a", "alice", "operator"))),
        encoding="utf-8",
    )
    link = tmp_path / "identities.json"
    link.symlink_to(target)

    with pytest.raises(
        FeishuIdentityConfigurationError,
        match=r"^feishu identity configuration invalid$",
    ):
        load_feishu_identity_directory(
            path=str(link), tenant_id="dev-local", environment_id="dev"
        )


def test_identity_fifo_without_a_writer_fails_closed_without_blocking(
    tmp_path: Path,
) -> None:
    fifo = tmp_path / "identities.pipe"
    os.mkfifo(fifo)
    script = """
import sys

from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityConfigurationError,
    load_feishu_identity_directory,
)

try:
    load_feishu_identity_directory(
        path=sys.argv[1], tenant_id="dev-local", environment_id="dev"
    )
except FeishuIdentityConfigurationError as error:
    assert str(error) == "feishu identity configuration invalid"
else:
    raise AssertionError("FIFO must be rejected")
"""

    completed = subprocess.run(  # noqa: S603 -- fixed interpreter and local FIFO path
        [sys.executable, "-c", script, str(fifo)],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
