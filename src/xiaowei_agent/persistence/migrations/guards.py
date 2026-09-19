"""Alembic 数据语义降级守卫。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context
from sqlalchemy.engine import Connection


class MigrationSafetyError(RuntimeError):
    """降级会丢失当前里程碑的数据语义。"""

    def __init__(self, *, counts: tuple[tuple[str, int], ...]) -> None:
        super().__init__("DESTRUCTIVE_DOWNGRADE_REJECTED")
        self.counts = counts


class MigrationPreconditionError(RuntimeError):
    """升级前置条件不满足，继续会重解释既有数据语义。"""

    def __init__(self, *, counts: tuple[tuple[str, int], ...]) -> None:
        super().__init__("MIGRATION_PRECONDITION_FAILED")
        self.counts = counts


def _row_counts(
    connection: Connection,
    *,
    guarded: Sequence[tuple[sa.FromClause, str]],
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (category, connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())
        for table, category in guarded
    )


def require_no_rows(
    connection: Connection,
    *,
    guarded: Sequence[tuple[sa.FromClause, str]],
) -> None:
    """升级前置条件：命中任何受保护旧语义行都拒绝继续。"""
    counts = _row_counts(connection, guarded=guarded)
    if any(count for _, count in counts):
        raise MigrationPreconditionError(counts=counts)


def require_destructive_authorization(
    connection: Connection,
    *,
    guarded: Sequence[tuple[sa.FromClause, str]],
) -> None:
    """有受保护数据时默认拒绝，只输出类别与数量。"""
    counts = _row_counts(connection, guarded=guarded)
    if not any(count for _, count in counts):
        return
    if context.config.attributes.get("allow_destructive", False) is not True:
        raise MigrationSafetyError(counts=counts)
