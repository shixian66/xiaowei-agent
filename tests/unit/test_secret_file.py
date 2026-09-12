"""共享 credential reader 的 descriptor 关闭与安全异常语义。"""

import stat
from types import SimpleNamespace

import pytest

from xiaowei_agent.interfaces import secret_file
from xiaowei_agent.interfaces.secret_file import SecretFileError, read_secret_file


def _install_file_ops(
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_error: OSError | None = None,
    close_error: OSError | None = None,
) -> list[int]:
    closed: list[int] = []
    monkeypatch.setattr(secret_file.os, "open", lambda path, flags: 17)
    monkeypatch.setattr(
        secret_file.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_mode=stat.S_IFREG),
    )

    def read(descriptor: int, limit: int) -> bytes:
        if read_error is not None:
            raise read_error
        return b"fake-value\n"

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        if close_error is not None:
            raise close_error

    monkeypatch.setattr(secret_file.os, "read", read)
    monkeypatch.setattr(secret_file.os, "close", close)
    return closed


def _assert_safe_secret_error(error: SecretFileError) -> None:
    assert str(error) == "secret file unavailable"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_secret_reader_closes_descriptor_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _install_file_ops(monkeypatch)

    assert read_secret_file("/run/secrets/fake") == "fake-value"
    assert closed == [17]


@pytest.mark.parametrize(
    ("read_error", "close_error"),
    [
        (OSError("private-read-detail"), None),
        (None, OSError("private-close-detail")),
        (OSError("private-read-detail"), OSError("private-close-detail")),
    ],
)
def test_secret_reader_normalizes_read_and_close_failures_without_leaking_chain(
    monkeypatch: pytest.MonkeyPatch,
    read_error: OSError | None,
    close_error: OSError | None,
) -> None:
    closed = _install_file_ops(
        monkeypatch,
        read_error=read_error,
        close_error=close_error,
    )

    with pytest.raises(SecretFileError) as caught:
        read_secret_file("/run/secrets/fake")

    _assert_safe_secret_error(caught.value)
    assert closed == [17]
