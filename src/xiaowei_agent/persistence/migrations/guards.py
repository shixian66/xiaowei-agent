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


def require_destructive_authorization(
    connection: Connection,
    *,
    guarded: Sequence[tuple[sa.FromClause, str]],
) -> None:
    """有受保护数据时默认拒绝，只输出类别与数量。"""
    counts = tuple(
        (category, connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())
        for table, category in guarded
    )
    if not any(count for _, count in counts):
        return
    if context.config.attributes.get("allow_destructive", False) is not True:
        raise MigrationSafetyError(counts=counts)
