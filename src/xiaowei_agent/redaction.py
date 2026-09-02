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

from pydantic import ValidationError

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


def safe_error_details(exc: "ValidationError") -> tuple[str, ...]:
    """把 ``ValidationError`` 投影成不含取值的 ``"位置: 类型"`` 序列。

    **这是 ``.errors()`` 的唯一合法出口。** ``model_config`` 里的
    ``hide_input_in_errors=True`` 只影响 ``str(exc)``——``exc.errors()`` 返回的
    字典里 ``input`` 仍然是**完整原始输入**。只开基类配置并不能关闭泄漏路径，
    这一点必须靠机制固定下来，因此 ``test_no_raw_validation_error_egress``
    禁止本函数之外的任何 ``.errors()`` 调用。

    只取 ``loc`` 与 ``type``：``type`` 是错误种类常量；``msg`` 在部分错误类型里会
    内联取值，因此不取。

    ``loc`` 需要额外说明：映射字段出错时，Pydantic 会把**出错的那个键**嵌进
    ``loc``（``detail.  token=secret  .[key]``），而映射的键恰恰全是外部文本。
    ``hide_input_in_errors`` 对 ``loc`` 无效。因此契约层的映射校验一律改为在
    ``BeforeValidator`` 内部手写、错误只用序号定位（见
    ``contracts.base._checked_map``）——本函数的 ``loc`` 之所以安全，靠的是那一侧
    的约束，而不是这里做了过滤。
    """
    return tuple(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['type']}"
        # include_input=False 在当前实现下**不承重**（下面只取 loc 与 type，
        # 根本不读 input），变异测试已确认去掉它测试仍全绿。保留它是为了限制
        # 未来扩展投影字段时的影响面——把它写成"这就是防线"会是错的。
        for item in exc.errors(include_input=False, include_url=False)
    )


def safe_exception_text(exc: BaseException) -> str:
    """把任意异常渲染成**永不再抛异常**且已脱敏的一行文本。

    ``f"{type(exc).__name__}: {exc}"`` 有两个独立缺陷：

    1. ``str(exc)`` 会调用异常自己的 ``__str__``。一个 ``__str__`` 抛异常的错误
       对象（故障的第三方 driver，或恶意实现）会让**异常处理分支本身再抛异常**，
       于是"任何 adapter 异常都被结构化吸收"这条承诺当场失效，原始异常还会一路
       逃逸到调用方。根因与"先派生后校验"相同：处理器假定某个操作是全函数。
    2. 渲染出来的文本是上游原文，可能带连接串、口令。

    ``type(exc).__name__`` 同样不假定为安全：元类可以让属性访问抛异常。因此整个
    渲染过程都在保护之下，任何一步失败都降级为 :data:`REDACTED`。
    """
    try:
        name = type(exc).__name__
    except Exception:
        name = REDACTED
    body = _safe_str(exc)
    try:
        return scrub_text(f"{name}: {body}")
    except Exception:
        return REDACTED
