"""SQLAlchemy 异常必须先归成不含驱动原文的闭集结果。"""

import pytest
import sqlalchemy as sa

from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
    PersistenceWriteOutcome,
    classify_persistence_exception,
)
from xiaowei_agent.persistence.postgres import _persistence_boundary


def _statement_error(error_type: type[sa.exc.StatementError]) -> sa.exc.StatementError:
    return error_type("constant", "SELECT 1", {}, RuntimeError("constant"))


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError(), PersistenceUnavailableCategory.TIMEOUT),
        (sa.exc.TimeoutError("constant"), PersistenceUnavailableCategory.POOL),
        (
            _statement_error(sa.exc.OperationalError),
            PersistenceUnavailableCategory.CONNECT,
        ),
        (
            _statement_error(sa.exc.InterfaceError),
            PersistenceUnavailableCategory.CONNECT,
        ),
        (sa.exc.DisconnectionError("constant"), PersistenceUnavailableCategory.TRANSIENT),
    ],
)
def test_unavailable_errors_are_classified(
    error: BaseException, category: PersistenceUnavailableCategory
) -> None:
    mapped = classify_persistence_exception(
        error, write_outcome=PersistenceWriteOutcome.ROLLED_BACK
    )
    assert isinstance(mapped, PersistenceUnavailableError)
    assert mapped.category is category
    assert mapped.write_outcome is PersistenceWriteOutcome.ROLLED_BACK


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (_statement_error(sa.exc.IntegrityError), PersistenceIntegrityCategory.CONSTRAINT),
        (_statement_error(sa.exc.DataError), PersistenceIntegrityCategory.DATA),
        (_statement_error(sa.exc.ProgrammingError), PersistenceIntegrityCategory.SCHEMA),
        (sa.exc.InvalidRequestError("constant"), PersistenceIntegrityCategory.SCHEMA),
    ],
)
def test_systemic_errors_are_classified(
    error: BaseException, category: PersistenceIntegrityCategory
) -> None:
    mapped = classify_persistence_exception(error)
    assert isinstance(mapped, PersistenceIntegrityError)
    assert mapped.category is category


def test_connection_invalidated_wins_over_statement_error() -> None:
    error = sa.exc.DBAPIError(
        "SELECT 1",
        {},
        ConnectionError("constant"),
        connection_invalidated=True,
    )
    mapped = classify_persistence_exception(
        error, write_outcome=PersistenceWriteOutcome.NOT_CONFIRMED
    )
    assert isinstance(mapped, PersistenceUnavailableError)
    assert mapped.category is PersistenceUnavailableCategory.TRANSIENT
    assert mapped.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED


def test_unrelated_errors_are_not_claimed_by_the_persistence_boundary() -> None:
    assert classify_persistence_exception(ValueError("constant")) is None


def test_mapped_errors_never_echo_driver_text() -> None:
    marker = "private-marker"
    mapped = classify_persistence_exception(
        sa.exc.ProgrammingError("statement", "SELECT :value", {"value": marker}, None)
    )
    assert isinstance(mapped, PersistenceIntegrityError)
    assert marker not in str(mapped)
    assert marker not in repr(mapped)


@pytest.mark.asyncio
async def test_postgres_write_boundary_cuts_the_driver_exception_chain() -> None:
    marker = "private-" + "statement-value"

    @_persistence_boundary(write=True)
    async def fail() -> None:
        raise sa.exc.OperationalError(
            "SELECT :value", {"value": marker}, ConnectionError(marker)
        )

    with pytest.raises(PersistenceUnavailableError) as caught:
        await fail()
    assert caught.value.write_outcome is PersistenceWriteOutcome.NOT_CONFIRMED
    assert caught.value.__context__ is None
    assert marker not in str(caught.value)
    assert marker not in repr(caught.value)


@pytest.mark.asyncio
async def test_postgres_read_boundary_has_no_write_outcome() -> None:
    @_persistence_boundary(write=False)
    async def fail() -> None:
        raise sa.exc.TimeoutError("constant")

    with pytest.raises(PersistenceUnavailableError) as caught:
        await fail()
    assert caught.value.write_outcome is None
