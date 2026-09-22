"""公开的旧身份文档解析契约：保留原始 labels，值域一字不放宽。

抽出这份契约的**唯一**理由是 ``load_feishu_identity_directory()`` 已经把 labels
压成了 ``frozenset[ChannelPermission]``：``viewer`` 与 ``approver`` 压完之后完全
一样，而规格 §6.6 的迁移表恰恰要按**原始 label** 分流。

因此本文件同时钉两件事：新入口拿得到原始标签，以及公开化**没有**顺手放宽任何
校验——后三条反例逐条对应"把 labels 写回 ``tuple[StrictStr, ...]``"会误放进来的
三类输入。
"""

import json
from pathlib import Path

import pytest

from xiaowei_agent.contracts import ChannelPermission, IdentitySource
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityConfigurationError,
    LegacyIdentityEntry,
    load_feishu_identity_directory,
    read_legacy_identity_document,
)

# 逐字复述实现之前就已生效的映射表。写在这里而不是 import 过来，是为了让"既有
# 调用方零行为变化"这条断言与实现各自独立——import 同一份常量时，改错了两边会
# 一起改，测试永远绿。
_PERMISSIONS_BEFORE: dict[str, frozenset[ChannelPermission]] = {
    "operator": frozenset(
        {ChannelPermission.VIEW_SAFE_TASK, ChannelPermission.SUBMIT_READONLY_TASK}
    ),
    "dba": frozenset(
        {ChannelPermission.VIEW_SAFE_TASK, ChannelPermission.SUBMIT_READONLY_TASK}
    ),
    "oncall": frozenset(
        {ChannelPermission.VIEW_SAFE_TASK, ChannelPermission.SUBMIT_READONLY_TASK}
    ),
    "viewer": frozenset({ChannelPermission.VIEW_SAFE_TASK}),
    "approver": frozenset({ChannelPermission.VIEW_SAFE_TASK}),
    "admin": frozenset(ChannelPermission),
}


def _entry(subject_ref: str, actor: str, *labels: str) -> dict[str, object]:
    return {"subject_ref": subject_ref, "actor": actor, "labels": list(labels)}


def _document(*entries: dict[str, object], **updates: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 1,
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "entries": list(entries),
    }
    return base | updates


def _write(tmp_path: Path, document: dict[str, object]) -> str:
    path = tmp_path / "identities.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _read(tmp_path: Path, document: dict[str, object]) -> tuple[LegacyIdentityEntry, ...]:
    return read_legacy_identity_document(
        path=_write(tmp_path, document), tenant_id="dev-local", environment_id="dev"
    )


def test_reading_preserves_the_original_labels(tmp_path: Path) -> None:
    entries = _read(
        tmp_path,
        _document(
            _entry("subject-alice", "alice", "viewer"),
            _entry("subject-bob", "bob", "approver"),
            _entry("subject-carol", "carol", "dba", "oncall"),
        ),
    )

    assert [entry.actor for entry in entries] == ["alice", "bob", "carol"]
    assert [entry.labels for entry in entries] == [
        ("viewer",),
        ("approver",),
        ("dba", "oncall"),
    ]
    # 这一对是抽出解析器的全部理由：压成权限位之后它们完全一样。
    assert _PERMISSIONS_BEFORE["viewer"] == _PERMISSIONS_BEFORE["approver"]


def test_reading_enforces_the_same_scope_check_as_before(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _document(
            _entry("subject-alice", "alice", "operator"),
            tenant_id="other-tenant",
        ),
    )

    with pytest.raises(
        FeishuIdentityConfigurationError,
        match=r"^feishu identity configuration invalid$",
    ):
        read_legacy_identity_document(
            path=path, tenant_id="dev-local", environment_id="dev"
        )


@pytest.mark.parametrize(
    "document",
    [
        _document(_entry("subject-alice", "alice", "operator"), version=2),
        _document(_entry("subject-alice", "alice", "operator"), extra="x"),
        _document(
            _entry("subject-alice", "alice", "operator"),
            _entry("subject-bob", "alice", "operator"),
        ),
    ],
    ids=["bad-version", "unknown-field", "duplicate-actor"],
)
def test_reading_never_echoes_the_document_on_failure(
    tmp_path: Path, document: dict[str, object]
) -> None:
    path = _write(tmp_path, document)

    with pytest.raises(FeishuIdentityConfigurationError) as caught:
        read_legacy_identity_document(
            path=path, tenant_id="dev-local", environment_id="dev"
        )

    message = str(caught.value)
    assert message == "feishu identity configuration invalid"
    assert "subject-alice" not in message
    assert "alice" not in message


def test_the_existing_directory_loader_still_behaves_identically(
    tmp_path: Path,
) -> None:
    document = _document(
        *(
            _entry(f"subject-{label}", label, label)
            for label in sorted(_PERMISSIONS_BEFORE)
        )
    )
    path = _write(tmp_path, document)

    directory = load_feishu_identity_directory(
        path=path, tenant_id="dev-local", environment_id="dev"
    )

    for label, expected in _PERMISSIONS_BEFORE.items():
        principal = directory.resolve(subject_ref=f"subject-{label}")
        assert principal.permissions == expected
        assert principal.actor == label
        assert principal.tenant_id == "dev-local"
        assert principal.environment_id == "dev"
        assert principal.source is IdentitySource.FEISHU
        assert principal.subject_ref == f"subject-{label}"

    # 两条路径读的是同一份文档、同一批主体。
    entries = read_legacy_identity_document(
        path=path, tenant_id="dev-local", environment_id="dev"
    )
    assert {entry.subject_ref for entry in entries} == {
        f"subject-{label}" for label in _PERMISSIONS_BEFORE
    }


def test_the_public_entry_hides_the_open_id(tmp_path: Path) -> None:
    (entry,) = _read(
        tmp_path, _document(_entry("subject-alice", "alice", "operator"))
    )

    # 值还在，两条外泄通道都关上了——它们是独立通道，少关一条不会有任何反馈。
    assert entry.subject_ref == "subject-alice"
    assert "subject-alice" not in repr(entry)
    assert "subject_ref" not in entry.model_dump()
    assert "subject-alice" not in entry.model_dump_json()


def test_empty_labels_are_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(FeishuIdentityConfigurationError):
        _read(tmp_path, _document(_entry("subject-alice", "alice")))


def test_an_unknown_label_is_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(FeishuIdentityConfigurationError):
        _read(tmp_path, _document(_entry("subject-alice", "alice", "superuser")))


def test_more_than_six_labels_are_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(FeishuIdentityConfigurationError):
        _read(
            tmp_path,
            _document(
                _entry(
                    "subject-alice",
                    "alice",
                    "operator",
                    "dba",
                    "oncall",
                    "viewer",
                    "approver",
                    "admin",
                    "operator",
                )
            ),
        )
