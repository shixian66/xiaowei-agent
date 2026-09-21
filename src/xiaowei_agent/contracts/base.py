"""全部跨模块契约的共同基座。

本模块解决三条**独立**的绕过路径，缺一条则"违反安全边界的构造在契约层不可
表达"就不成立：

1. ``frozen=True`` 只挡属性重绑定，挡不住内部 ``dict`` 被原地改写 →
   映射字段一律用 :data:`FrozenMap`：**先 ``dict()`` 复制**（切断与调用方原对象
   的联系），**再用 ``MappingProxyType`` 包装**（挡住原地改写）。两步缺一不可
   ——只包装不复制等于给外部 dict 套视图，外部一改内部就变。
2. ``model_copy(update=...)`` **完全不触发校验**——``Field`` 约束、
   ``model_validator`` 与 ``AfterValidator`` 全部被跳过 → :class:`Contract`
   覆盖它，带 ``update`` 时重新走 ``model_validate``。
3. ``model_construct`` 跳过**全部**校验，比 ``model_copy`` 更彻底 → 一并封死。

**本类不提供任何"未校验复制"的逃生口。** 需要在校验期规范化取值时，一律用
**字段级** ``AfterValidator``——``model_validator(mode="after")`` 里返回非
``self`` 的对象在 ``__init__`` 路径上会被丢弃（Pydantic 只发一条警告），规范化
会静默失效。``tests/security/test_validation_bypass_surface.py`` 断言这条逃生口
始终不存在。

重新校验用 ``{**self.__dict__, **update}`` 而非 ``model_dump()``，以保留嵌套契约
实例与 ``datetime`` 的原始类型，避免 JSON 往返丢失信息。

**Pydantic 默认 lax 模式的隐式转换同样是绕过**：``bool`` 接受 ``"yes"`` / ``1``，
``int`` 接受 ``"2"`` / ``True``，``str`` 接受 ``bytes``。一个被构造成
``allow="yes"`` 的 ``PolicyDecision`` 会在 Gateway 处真的放行。

因此 **``strict=True`` 提到基类，严格性是默认而非逐字段选择加入**。逐字段开启的
做法必然会漏——本项目已经漏过一轮：``StrictInt`` 定义了却没用到 ``PolicyDecision``、
``AnswerabilityVerdict``、``EvidenceEnvelope`` 等处的 ``bool`` 上。

**这不会打断 M4 的反序列化**：``str -> StrEnum``、ISO 字符串 -> ``datetime``、
``list -> tuple`` 在 strict 下确实被 python 校验模式拒绝，但在 **JSON 校验模式**
（``model_validate_json``）下依然允许，而 ``bool`` / ``int`` 的严格性在两种模式下
都保持。TaskStore 往返走的正是 JSON 路径，因此两者可以兼得——已实证。

**已知取舍**：``MappingProxyType`` 不可哈希，因此携带映射字段的契约实例不可作为
dict 键或放入 set。本项目不依赖契约的可哈希性，比较一律用 ``==``。
"""

import datetime as _dt
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Final, Never, Self, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
)


def _require_aware(value: _dt.datetime) -> _dt.datetime:
    """拒绝 naive datetime。

    naive 与 aware 相比较会抛 ``TypeError``——审批过期判定会因此变成一个非结构化
    的崩溃，而不是 fail-closed 的拒绝。时间边界一律要求带时区。
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware")
    return value


AwareDatetime: TypeAlias = Annotated[_dt.datetime, AfterValidator(_require_aware)]
"""带时区的时间戳。全部跨边界时间字段一律使用本别名。"""


def _require_true(value: bool) -> bool:
    if value is not True:
        raise ValueError("must be exactly True")
    return value


AlwaysTrue: TypeAlias = Annotated[bool, AfterValidator(_require_true)]
"""恒为 ``True`` 的标志位。

**不要用 ``Literal[True]``**：``Literal`` 按 ``==`` 比较，而 ``1 == True`` 为真，
即使在严格模式下 ``1`` 也会被接受——"只读证据"于是可以由一个整数冒充。
``bool`` + ``is True`` 校验才同时挡住 ``1``（严格 bool）与 ``False``（校验器）。
"""
"""带时区的时间戳。全部跨边界时间字段一律使用本别名。"""

TRACE_ID_PATTERN: Final[str] = r"[0-9a-f]{32}"
"""trace_id 的字面格式，全项目**唯一**定义处。

``trace.py`` 反向引用本常量：格式若在两处各写一份，生成端与校验端会悄悄漂移。
不写成 Pydantic 的 ``pattern=``——pydantic v2 用 Rust regex 引擎，不支持 ``\\A`` /
``\\Z`` 锚点，而 ``^``/``$`` 在该引擎下的多行语义与 Python 不一致；统一用
``re.fullmatch`` 校验，语义确定。
"""

_TRACE_ID_RE: Final[re.Pattern[str]] = re.compile(TRACE_ID_PATTERN)


def _trace_id(value: str) -> str:
    if not _TRACE_ID_RE.fullmatch(value):
        raise ValueError("trace_id must be 32 lowercase hex characters")
    return value


TraceId = Annotated[str, AfterValidator(_trace_id)]

StrictInt: TypeAlias = int
"""整数别名。

严格性由 :class:`Contract` 的 ``model_config`` 统一提供，因此这里不再重复
``Field(strict=True)``。保留别名是为了让"这是一个跨边界标量"在签名上仍然可读。
"""

FiniteFloat: TypeAlias = Annotated[float, Field(allow_inf_nan=False)]
"""禁止 NaN / ±Inf 的浮点。

**必须在 DTO 构造阶段拒绝，而不是等到 ``canonical_json``**：
``AdapterResponse.payload`` 与 ``EvidenceEnvelope.facts`` 根本不经过
``canonical_json``，NaN 会一路流进证据与渲染；而 ``PlanStep.typed_arguments``
里的 NaN 要到准入时刻算 ``plan_hash`` 才抛错，那时计划已经存进 TaskStore 了。
``canonical_json`` 的同名检查保留，作为第二道防线。
"""

def _strict_str(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("must not be empty or padded with whitespace")
    return value


# ``strict=True`` 的另一层作用：lax 模式下 ``str`` 会接受 ``bytes`` 并解码，
# 使 id、trace_id、槽位这类字段能被二进制内容填充。
#
# 三个字符串别名按**语义**分层，不能互相替代；``test_string_alias_coverage``
# 强制每个 ``str`` 字段显式落在其中之一：
#
# * ``StrictStr``   —— 标识符与引用（id、ref、hash、reason code）。空串与带空白
#   的值必须拒绝：``approval_ref=""`` 在结构上"存在"（不是 None）却在所有真值
#   判断里"不存在"，这种二义性正是 fail-closed 要消灭的；``" r1 "`` 与 ``"r1"``
#   则是同一个引用的两个别名，会让引用相等性失效。
# * ``NonEmptyText`` —— 由本系统确定性生成的展示/说明文本。空串是缺陷，但**不做
#   strip 校验**：文本的前后空白属于内容本身。
# * ``FreeText``    —— 外部不可信文本逐字捕获。空串与空白都是合法内容，任何
#   收紧都会篡改被捕获的原文，违背 ExternalContent 的逐字语义。
StrictStr = Annotated[str, Field(strict=True), AfterValidator(_strict_str)]


def _non_empty(value: str) -> str:
    if not value:
        raise ValueError("must not be empty")
    return value


NonEmptyText = Annotated[str, Field(strict=True), AfterValidator(_non_empty)]

FreeText = Annotated[str, Field(strict=True)]

JsonScalar: TypeAlias = bool | int | FiniteFloat | FreeText | None
"""JSON 标量。

``str`` 分支写成 ``FreeText``：这些值来自 adapter 返回的行、证据事实与
typed_args，空串与空白都是合法数据（NULL 样式的空单元格）。写成裸 ``str``
会让别名内部成为"没有选档"的洞——AST 扫描只看字段标注，看不进别名里。
"""


SHA256_HEX_PATTERN: Final[str] = r"[0-9a-f]{64}"


def _sha256_hex(value: str) -> str:
    """摘要字段必须在**构造**时就是合法摘要形状。

    ``plan_hash="different"`` 这类值在旧标注下能构造成功，只在后续比对时表现为
    "不匹配"——与"计划确实变了"无法区分。形状在入口拒绝，两者才分得开。
    """
    if re.fullmatch(SHA256_HEX_PATTERN, value) is None:
        raise ValueError("must be a lowercase 64-char sha256 hex digest")
    return value


Sha256Hex = Annotated[str, Field(strict=True), AfterValidator(_sha256_hex)]


SecretHash = StrictStr
"""口令哈希的标记类型。

名字以 ``Secret`` 开头是有意义的：凡是这样标注的字段都必须同时写
``exclude=True`` 与 ``repr=False``（见 ``tests/security/test_secret_field_exposure.py``，
它按 AST 扫**注解名**发现字段，不按字段名）。哈希不是明文口令，但它是凭据
材料——进了日志或响应体就等于把离线爆破的输入交出去。

它住在 ``contracts/base.py`` 而不是 ``persistence/`` ：``contracts`` 是分层的叶子，
不能反向依赖 ``persistence``，而两边各写一份别名会让"同一套约定"变成两套。
"""

ControlledPii = StrictStr
"""受控 PII 的标记类型（飞书 ``open_id`` 这类外部主体标识）。

与 :data:`SecretHash` 同一套约定：凡这样标注的字段必须同时写 ``exclude=True``
与 ``repr=False``，由 ``tests/security/test_controlled_pii_exposure.py`` 承重。

它不叫 ``Secret*``，因为它不是凭据：泄露 ``open_id`` 不会让人登录，但会把
"这个人是谁"交出去。两类义务相同、理由不同，因此分两个标记类型、两条用例。
"""


def frozen_map(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """复制并包装为只读映射。"""
    return MappingProxyType(dict(value))


def _checked_map(
    value: object, *, key_adapter: TypeAdapter[Any], value_adapter: TypeAdapter[Any], label: str
) -> Mapping[str, Any]:
    """逐项校验映射的键与值，**错误文本里只出现序号，不出现取值**。

    为什么不写成 ``Mapping[StrictStr, JsonScalar]`` 让 Pydantic 自己校验：那样
    做时 ``ValidationError`` 的 ``loc`` 会把**出错的那个键原样嵌进去**，例如
    ``detail.  token=secret  .[key]``。``hide_input_in_errors`` 只隐藏 ``input``，
    对 ``loc`` 无效，因此原始键会出现在 ``str(exc)``、traceback 与任何基于
    ``loc`` 的"安全"投影里——``safe_error_details`` 也不例外。

    映射的键正是外部文本：trace detail 的键由调用方拼装、slots 的键来自模型
    输出、typed_args 的键来自计划编译。用序号定位在诊断上略差，但这是唯一能
    让拒绝路径不携带外部数据的写法。

    与 ``TraceDetail`` 的长度检查同一个根因：把约束交给内层值类型，就同时交出了
    执行顺序与错误内容的控制权。
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    out: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        try:
            safe_key = key_adapter.validate_python(key, strict=True)
        except ValidationError:
            raise ValueError(f"{label}: key #{index} is not a valid identifier") from None
        try:
            out[safe_key] = value_adapter.validate_python(item, strict=True)
        except ValidationError:
            raise ValueError(f"{label}: value at key #{index} is invalid") from None
    return MappingProxyType(out)


# ``PlainSerializer(dict)``：``MappingProxyType`` 不是 pydantic 认识的序列化目标，
# 不显式转换时 ``model_dump()`` 会发 PydanticSerializationUnexpectedValue 警告。
# M4 要把契约持久化进 TaskStore，序列化必须是干净的。
_as_dict = PlainSerializer(dict, return_type=dict, when_used="always")

_KEY_ADAPTER: TypeAdapter[str] = TypeAdapter(StrictStr)
_SCALAR_ADAPTER: TypeAdapter[Any] = TypeAdapter(JsonScalar)
_STR_ADAPTER: TypeAdapter[str] = TypeAdapter(StrictStr)

FrozenMap = Annotated[
    Mapping[StrictStr, JsonScalar],
    BeforeValidator(
        lambda v: _checked_map(
            v, key_adapter=_KEY_ADAPTER, value_adapter=_SCALAR_ADAPTER, label="mapping"
        )
    ),
    # 前置校验返回的 MappingProxyType 会被 Pydantic 的 Mapping 校验重建成普通
    # dict——只有前置校验时深不可变性会静默失效。因此必须在**内层校验之后**
    # 再冻结一次：前置管错误内容，后置管不可变性，两者不可互相替代。
    AfterValidator(frozen_map),
    _as_dict,
]
FrozenStrMap = Annotated[
    Mapping[StrictStr, StrictStr],
    BeforeValidator(
        lambda v: _checked_map(
            v, key_adapter=_KEY_ADAPTER, value_adapter=_STR_ADAPTER, label="mapping"
        )
    ),
    # 前置校验返回的 MappingProxyType 会被 Pydantic 的 Mapping 校验重建成普通
    # dict——只有前置校验时深不可变性会静默失效。因此必须在**内层校验之后**
    # 再冻结一次：前置管错误内容，后置管不可变性，两者不可互相替代。
    AfterValidator(frozen_map),
    _as_dict,
]


class Contract(BaseModel):
    """不可变、拒绝未声明字段、复制即重新校验的契约基类。"""

    # ``hide_input_in_errors=True`` 是**安全配置，不是可读性偏好**：默认的
    # ValidationError 会把被拒绝的原始输入回填进 ``errors()[i]["input"]`` 与
    # ``str(exc)``。契约层校验的正是外部文本、槽位、typed_args、trace detail
    # 这类可能携带 secret 的值，而拒绝路径的异常最终会进 traceback、日志与
    # 错误响应——于是"被拒绝"反而成了原文外泄的通道。
    #
    # 必须放在基类：逐个 DTO 配置会以与 P0-1（严格性逐字段选择加入）完全相同
    # 的方式漏掉下一个新增契约。
    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", hide_input_in_errors=True
    )

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """带 ``update`` 的复制必须重新校验。

        Pydantic 原生实现直接写字段、跳过全部校验，使任何"构造时不可表达"的
        约束都能被一次 copy 绕过。

        :raises ValidationError: update 后的取值违反任何字段或模型级约束。
        """
        if not update:
            return super().model_copy(deep=deep)
        return type(self).model_validate({**self.__dict__, **update})

    @classmethod
    def model_construct(cls, *args: object, **kwargs: object) -> Never:
        """封死：它跳过全部校验，是与 ``model_copy`` 同类的绕过通道。

        实证封死它不影响 ``model_validate``、``model_copy()``、``model_dump()``
        等正常路径。
        """
        raise NotImplementedError(
            "Contract 禁止 model_construct：它跳过全部校验，请用 model_validate"
        )
