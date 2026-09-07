"""为 M6b 带外 discovery 材料计算 runtime 同源摘要。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from typing import Never, TextIO

from xiaowei_agent.tools.starrocks import preflight_digest

_MAX_INPUT_BYTES = 1_048_576


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise ValueError("invalid command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(add_help=False)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--file")
    parser.add_argument("--order-insensitive", action="store_true")
    return parser


def _read_regular_file(path: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("input is not a regular file")
        payload = os.read(descriptor, _MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ValueError("input file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return payload


def _load_values(payload: bytes) -> tuple[str, ...]:
    if not payload or len(payload) > _MAX_INPUT_BYTES:
        raise ValueError("input shape is invalid")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("input shape is invalid") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) for item in decoded)
    ):
        raise ValueError("input shape is invalid")
    return tuple(decoded)


def main(argv: Sequence[str] | None = None, *, stdin: TextIO = sys.stdin) -> int:
    """只输出 SHA-256；任何失败只输出固定错误，不回显输入或路径。"""
    try:
        args = _parser().parse_args(argv)
        payload = (
            stdin.read(_MAX_INPUT_BYTES + 1).encode("utf-8")
            if args.stdin
            else _read_regular_file(args.file)
        )
        values = _load_values(payload)
        digest = preflight_digest(values, order_insensitive=args.order_insensitive)
    except (TypeError, ValueError):
        print("invalid digest input", file=sys.stderr)
        return 2
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
