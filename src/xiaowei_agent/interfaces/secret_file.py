"""本地 credential 文件的唯一、无 SDK 依赖读取边界。"""

import os
import stat
import unicodedata
from typing import Final

_MAX_SECRET_BYTES: Final[int] = 4096


class _SecretFileError(RuntimeError):
    """credential 引用无法安全读取。"""


def _read_secret_file(path: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise _SecretFileError("secret file unavailable")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    failed = False
    payload = b""
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed = True
        else:
            payload = os.read(descriptor, _MAX_SECRET_BYTES + 1)
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if failed or not payload or len(payload) > _MAX_SECRET_BYTES:
        raise _SecretFileError("secret file unavailable")
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        value = ""
        failed = True
    if value.endswith("\n"):
        value = value[:-1]
    if failed or not value or any(
        unicodedata.category(character) == "Cc" for character in value
    ):
        raise _SecretFileError("secret file unavailable")
    return value
