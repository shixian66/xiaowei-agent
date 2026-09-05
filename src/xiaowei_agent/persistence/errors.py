"""PostgreSQL adapter 的闭集错误分类；异常消息不携带驱动原文。"""

from enum import StrEnum

import sqlalchemy as sa
from pydantic import ValidationError


class PersistenceWriteOutcome(StrEnum):
    ROLLED_BACK = "rolled_back"
    NOT_CONFIRMED = "not_confirmed"


class PersistenceUnavailableCategory(StrEnum):
    CONNECT = "connect"
    TIMEOUT = "timeout"
    POOL = "pool"
    TRANSIENT = "transient"


class PersistenceIntegrityCategory(StrEnum):
    CONSTRAINT = "constraint"
    SCHEMA = "schema"
    DATA = "data"
    DECODE = "decode"


class PersistenceUnavailableError(RuntimeError):
    """连接/超时故障；写路径必须说明结果是否已确认回滚。"""

    def __init__(
        self,
        *,
        category: PersistenceUnavailableCategory,
        write_outcome: PersistenceWriteOutcome | None = None,
    ) -> None:
        super().__init__("persistence unavailable")
        self.category = category
        self.write_outcome = write_outcome


class PersistenceIntegrityError(RuntimeError):
    """schema、约束或解码故障；调用方必须 fail-stop，不能改任务。"""

    def __init__(self, *, category: PersistenceIntegrityCategory) -> None:
        super().__init__("persistence integrity failure")
        self.category = category


def classify_persistence_exception(
    exc: BaseException,
    *,
    write_outcome: PersistenceWriteOutcome | None = None,
) -> PersistenceUnavailableError | PersistenceIntegrityError | None:
    """按最具体到最宽的顺序映射；``None`` 表示不属于持久化边界。"""
    if isinstance(exc, sa.exc.DBAPIError) and exc.connection_invalidated:
        return PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.TRANSIENT,
            write_outcome=write_outcome,
        )
    if isinstance(exc, sa.exc.DisconnectionError):
        return PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.TRANSIENT,
            write_outcome=write_outcome,
        )
    if isinstance(exc, TimeoutError):
        return PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.TIMEOUT,
            write_outcome=write_outcome,
        )
    if isinstance(exc, sa.exc.TimeoutError):
        return PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.POOL,
            write_outcome=write_outcome,
        )
    if isinstance(exc, (sa.exc.OperationalError, sa.exc.InterfaceError)):
        return PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.CONNECT,
            write_outcome=write_outcome,
        )
    if isinstance(exc, sa.exc.IntegrityError):
        return PersistenceIntegrityError(category=PersistenceIntegrityCategory.CONSTRAINT)
    if isinstance(exc, sa.exc.DataError):
        return PersistenceIntegrityError(category=PersistenceIntegrityCategory.DATA)
    if isinstance(exc, sa.exc.ProgrammingError):
        return PersistenceIntegrityError(category=PersistenceIntegrityCategory.SCHEMA)
    if isinstance(exc, sa.exc.StatementError):
        return PersistenceIntegrityError(category=PersistenceIntegrityCategory.DATA)
    if isinstance(exc, sa.exc.SQLAlchemyError):
        return PersistenceIntegrityError(category=PersistenceIntegrityCategory.SCHEMA)
    if isinstance(exc, ValidationError):
        return PersistenceIntegrityError(category=PersistenceIntegrityCategory.DECODE)
    return None
