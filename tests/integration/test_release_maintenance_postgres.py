"""W5 一次性维护命令在真实 PostgreSQL 上的往返：只输出计数，重跑零创建。"""

import asyncio
import io
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xiaowei_agent.interfaces import legacy_identity_migration
from xiaowei_agent.persistence.schema import (
    ADMIN_AUDIT_EVENTS,
    EXTERNAL_IDENTITIES,
    USER_ACCOUNTS,
)

_ALICE = "ou_" + "pg-alice"
_BOB = "ou_" + "pg-bob"


def _document(tmp_path: Path) -> Path:
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": [
                    {"subject_ref": _ALICE, "actor": "pg-alice", "labels": ["operator"]},
                    {"subject_ref": _BOB, "actor": "pg-bob", "labels": ["approver"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


async def _counts(engine) -> list[int]:
    counts: list[int] = []
    async with engine.connect() as connection:
        for table in (USER_ACCOUNTS, EXTERNAL_IDENTITIES, ADMIN_AUDIT_EVENTS):
            counts.append(
                int(
                    await connection.scalar(
                        sa.select(sa.func.count()).select_from(table)
                    )
                    or 0
                )
            )
    return counts


async def test_identity_command_migrates_once_and_reruns_as_zero_created(
    clean_database, postgres_dsn, clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("XIAOWEI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XIAOWEI_ENVIRONMENT_ID", "dev")
    document = _document(tmp_path)

    async def run() -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        code = await asyncio.to_thread(
            legacy_identity_migration.main,
            [],
            stdout=stdout,
            stderr=stderr,
            engine_factory=lambda _settings: create_async_engine(postgres_dsn),
            clock=clock,
            document_path=str(document),
        )
        return code, stdout.getvalue(), stderr.getvalue()

    first = await run()
    after_first = await _counts(clean_database)
    second = await run()

    assert first[0] == 0, first[2]
    assert json.loads(first[1]) == {
        "status": "migrated",
        "created_count": 2,
        "skipped_count": 0,
        "deferred_count": 1,
    }
    assert second[0] == 0, second[2]
    assert json.loads(second[1]) == {
        "status": "migrated",
        "created_count": 0,
        "skipped_count": 2,
        "deferred_count": 1,
    }
    assert await _counts(clean_database) == after_first
    assert after_first[:2] == [2, 2]
    output = first[1] + first[2] + second[1] + second[2]
    for leaked in (_ALICE, _BOB, "pg-alice", "pg-bob", "approver"):
        assert leaked not in output
