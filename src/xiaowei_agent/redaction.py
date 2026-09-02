"""脱敏规则的单一真源。

从 ``log.py`` 下沉为顶层叶子模块，使 ``contracts`` 可以复用同一套规则而不依赖
logging。``log.py`` 反向 import 并原样 re-export，因此对外签名不变，M1 的脱敏
回归测试无需改动。

**本模块不 import 任何 ``xiaowei_agent`` 模块**——它是分层的最底层，
`contracts` 只允许依赖它与标准库。

**净化范围**：自由文本的键值对、认证方案与值形状三类；映射的键强制转为已脱敏
字符串；未知类型一律先 ``str()`` 再脱敏，避免绕过。

**过度脱敏优于泄漏**：未加引号的敏感值一律脱敏到分隔符或行尾。
"""

import re
from collections.abc import Mapping, Sequence
from typing import Final

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
"""``redact`` 的返回类型：保证 ``json.dumps`` 可直接序列化。"""

REDACTED: Final[str] = "***"
_MAX_DEPTH: Final[int] = 6

_KEY_WORDS: Final[str] = (
    r"password|passwd|secret|token|api[_-]?key|apikey|authorization|auth"
    r"|credential|cookie|connection_string|conn_str|private_key|access_key"
)
_KEY_RE: Final[re.Pattern[str]] = re.compile(_KEY_WORDS, re.IGNORECASE)

# 键可带引号；值可为带引号串、认证方案+凭证、或未加引号的一段（到分隔符/行尾）。
_TEXT_PAIR_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?i)(?P<q>[\"']?)(?P<key>{_KEY_WORDS})(?P=q)(?P<sep>\s*[:=：＝]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^,;}\]\n]+)"
)
_AUTH_SCHEME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(Basic|Bearer|Digest|Token|APIKey|Negotiate|NTLM)\s+[A-Za-z0-9+/=._~\-]{8,}"
)
_VALUE_SHAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@\S+"
    r"|\b(?:sk-|ghp_|gho_|ghs_|github_pat_|AKIA|ASIA)[A-Za-z0-9_\-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _safe_str(value: object) -> str:
    """字符串化任意对象；``__str__``/``__repr__`` 抛异常时降级为 :data:`REDACTED`。

    日志绝不能因为被记录对象自身出错而抛异常到调用方，或丢失整条记录。
    """
    try:
        return str(value)
    except Exception:
        return REDACTED


def scrub_text(text: str) -> str:
    """对自由文本做键值、认证方案与值形状三类脱敏。"""
    text = _TEXT_PAIR_RE.sub(
        lambda m: f"{m.group('q')}{m.group('key')}{m.group('q')}{m.group('sep')}{REDACTED}", text
    )
    text = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _VALUE_SHAPE_RE.sub(REDACTED, text)


def redact(value: object, *, _depth: int = 0) -> JsonValue:
    """返回脱敏且 **JSON 可序列化** 的新对象；不修改入参。

    未知类型一律先 ``str()`` 再脱敏，避免绕过；映射的键强制转为已脱敏字符串，
    避免 ``json.dumps`` 因非法键类型抛错而丢失整条记录。
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bytes | bytearray):
        return REDACTED
    if isinstance(value, Mapping):
        # 获取、迭代、解包、转换必须全部在同一个 fail-closed 边界内：
        # `items()` 可能抛异常，也可能返回无法解包为二元组的元素。
        try:
            out: dict[str, JsonValue] = {}
            for key, item in value.items():
                skey = scrub_text(key if isinstance(key, str) else _safe_str(key))
                out[skey] = REDACTED if _KEY_RE.search(skey) else redact(item, _depth=_depth + 1)
            return out
        except Exception:
            return REDACTED
    if isinstance(value, Sequence | set | frozenset):
        try:
            return [redact(v, _depth=_depth + 1) for v in value]
        except Exception:
            # 迭代自定义序列/集合时同样可能抛异常。
            return REDACTED
    return scrub_text(_safe_str(value))
