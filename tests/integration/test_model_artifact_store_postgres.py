"""ModelArtifactStore PostgreSQL 共享绑定。"""

import json
from typing import Any

import sqlalchemy as sa
from tests.suites.model_artifacts import (
    MODEL_ARTIFACT_CASES,
    _grant,
    _interaction_candidate,
    bind,
)

bind(globals(), MODEL_ARTIFACT_CASES)


async def _insert_legacy_v1_interaction(
    clean_database: Any, *, task_id: str, fencing_token: int
) -> None:
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO task_interaction_artifacts "
                "(task_id, artifact_version, draft, origin, provider, model, "
                "provider_origin, prompt_revision, schema_revision, input_digest, "
                "result_digest, usage, created_at, fencing_token) VALUES "
                "(:task_id, 1, CAST(:draft AS jsonb), 'rule', NULL, NULL, NULL, "
                "'legacy-intent-prompt', 'legacy-intent-schema', :input_digest, "
                ":result_digest, CAST(:usage AS jsonb), now(), :fencing_token)"
            ),
            {
                "task_id": task_id,
                "draft": json.dumps({"source": "user", "operation": "inspect"}),
                "input_digest": "1" * 64,
                "result_digest": "2" * 64,
                "usage": json.dumps({"input_tokens": 0, "output_tokens": 0}),
                "fencing_token": fencing_token,
            },
        )


async def test_legacy_v1_interaction_row_is_not_loaded_as_v2(
    model_artifact_store: Any,
    store: Any,
    context: Any,
    clean_database: Any,
) -> None:
    grant = await _grant(store, context, key="legacy-load-filter")
    await _insert_legacy_v1_interaction(
        clean_database, task_id=grant.task_id, fencing_token=grant.fencing_token
    )

    assert await model_artifact_store.load_interaction(task_id=grant.task_id) is None


async def test_legacy_v1_interaction_row_is_replaced_by_current_v2_write(
    model_artifact_store: Any,
    store: Any,
    context: Any,
    clean_database: Any,
) -> None:
    grant = await _grant(store, context, key="legacy-replace-v2")
    await _insert_legacy_v1_interaction(
        clean_database, task_id=grant.task_id, fencing_token=grant.fencing_token
    )

    saved = await model_artifact_store.save_interaction(
        grant=grant, candidate=_interaction_candidate()
    )

    assert saved.artifact_version == 2
    assert await model_artifact_store.load_interaction(task_id=grant.task_id) == saved
    async with clean_database.connect() as connection:
        versions = (
            await connection.execute(
                sa.text(
                    "SELECT artifact_version FROM task_interaction_artifacts "
                    "WHERE task_id = :task_id ORDER BY artifact_version"
                ),
                {"task_id": grant.task_id},
            )
        ).all()
    assert versions == [(2,)]
