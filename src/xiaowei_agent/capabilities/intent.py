"""意图解释：自然语言 → ``IntentDraft``。

``IntentDraft`` 是全项目不可信度最高的 DTO。**解释器不拥有任何执行权**：它产出
的 intent 只是线索，最终 capability 由 ``CapabilityResolver`` 从注册快照确定性
地重新得到（ARCHITECTURE §4.1）。

因此本模块的安全责任只有一条：**产出的槽位键必须落在闭集内**。做法不是"识别并
剔除危险键"，而是只从闭集里逐个尝试填充——多余的键在结构上就无法出现，不依赖
下游记得忽略。

M3 的实现是规则式的，不调用任何模型（ADR-007 D4：M0-M6a 禁止真实模型 API 调用）。
"""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Protocol

from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext
from xiaowei_agent.contracts.intent import (
    ASSET_INVENTORY_INTENT,
    INTENT_SLOT_ALLOWLISTS,
    PROMETHEUS_ALERT_INTENT,
    SLOW_QUERY_INTENT,
    UNKNOWN_INTENT,
)

_IDENTIFIER: Final[str] = r"[A-Za-z0-9_][A-Za-z0-9_$-]{0,63}"
"""槽位取值的形状：朴素标识符。

与 ``planning.starrocks.params.Identifier`` 同一形状。这一层不是最终防线——
参数层才是——但让引号、分号、空格、注释符根本进不了 ``IntentDraft``，可以使
后续每一层都不必先做清洗。
"""

# 每个槽位一条确定性规则。键取自闭集，取值形状受 _IDENTIFIER 约束。
_SLOT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("database", re.compile(rf"(?:database|db)\s*=\s*({_IDENTIFIER})")),
    ("database", re.compile(rf"({_IDENTIFIER})\s*库")),
    ("user_name", re.compile(rf"(?:user_name|user)\s*=\s*({_IDENTIFIER})")),
    ("user_name", re.compile(rf"用户\s*({_IDENTIFIER})")),
    ("query_id", re.compile(rf"(?:query_id|queryId)\s*=\s*({_IDENTIFIER})")),
    ("environment_id", re.compile(rf"(?:environment_id|env)\s*=\s*({_IDENTIFIER})")),
)

_PROMETHEUS_SLOT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "alert_name",
        re.compile(
            r"(?:告警名\s*=\s*|告警\s+)(HostHighCpu|InstanceDown)(?=\s|在|$)"
        ),
    ),
    ("instance", re.compile(r"instance\s*=\s*([^\s,，]+)")),
    ("instance", re.compile(r"在\s*([A-Za-z0-9.\-\[\]:]+)(?=\s|$)")),
    ("instance", re.compile(r"查\s*([A-Za-z0-9.\-\[\]:]+)\s*的告警")),
    (
        "fingerprint",
        re.compile(r"fingerprint\s*=\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})"),
    ),
)

_ASSET_SLOT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("asset_id", re.compile(r"(?:asset_id|资产ID)\s*=\s*([^\s,，]+)", re.I)),
    ("hostname", re.compile(r"(?:hostname|主机名)\s*=\s*([^\s,，]+)", re.I)),
    ("ip", re.compile(r"(?:ip|IP地址)\s*=\s*([^\s,，]+)", re.I)),
)

_WINDOW_MINUTES: Final[re.Pattern[str]] = re.compile(
    r"(?:window_minutes\s*=\s*|最近\s*)(\d{1,5})\s*分钟"
)
_WINDOW_HOURS: Final[re.Pattern[str]] = re.compile(r"最近\s*(\d{1,3})\s*小时")

_MINUTES_PER_HOUR: Final[int] = 60
_PROMETHEUS_OTHER_SURFACES: Final[tuple[str, ...]] = (
    "grafana",
    "dashboard",
    "巡检",
)
_PROMETHEUS_WRITE_PREFIXES: Final[tuple[str, ...]] = (
    "静默",
    "创建",
    "关闭",
    "确认",
    "删除",
    "修改",
)
_ASSET_SELECTOR_SLOTS: Final[frozenset[str]] = INTENT_SLOT_ALLOWLISTS[
    ASSET_INVENTORY_INTENT
]
_ASSET_EXCLUDED_MARKERS: Final[tuple[str, ...]] = (
    "全部",
    "列出",
    "模糊",
    "网段",
    "巡检",
    "拓扑",
    "修改",
    "删除",
    "创建",
)


class IntentInterpreter(Protocol):
    """自然语言到结构化草案的唯一入口。

    实现可以使用模型，但输出必须是带 schema 的 ``IntentDraft``；模型产生的意图、
    目标线索与置信信息一律是不可信输入。
    """

    def interpret(self, *, text: str, context: RequestContext) -> IntentDraft: ...


class RuleBasedIntentInterpreter:
    """确定性规则解释器。

    同一输入必然产生逐字段相同的草案——这是"固定输入产生同一计划"这条退出标准
    在链路最上游的前提。
    """

    SLOT_ALLOWLISTS: Final[Mapping[str, frozenset[str]]] = INTENT_SLOT_ALLOWLISTS
    """每个意图各自的槽位闭集。

    先判唯一意图，再只运行该意图的提取器。这样资产或 SQL 槽位不会因为全局扫描
    混入 Prometheus 草案；执行权字段在所有集合中都不存在。
    """

    REQUIRED_SLOTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
        {
            SLOW_QUERY_INTENT: frozenset(),
            PROMETHEUS_ALERT_INTENT: frozenset({"alert_name", "instance"}),
            ASSET_INVENTORY_INTENT: frozenset(),
            UNKNOWN_INTENT: frozenset(),
        }
    )

    _RECOGNISED_CONFIDENCE: Final[float] = 0.9
    _UNRECOGNISED_CONFIDENCE: Final[float] = 0.0

    def interpret(self, *, text: str, context: RequestContext) -> IntentDraft:
        """按闭集规则产出草案。

        :param text: 用户原文，按不可信外部文本处理。
        :param context: 执行上下文；规则解释器只识别用户明示槽位，不改写上下文。
        """
        del context
        intent = self._recognise_intent(text)
        slots = self._extract_slots(text, intent=intent)
        recognised = intent != UNKNOWN_INTENT
        missing = tuple(sorted(self.REQUIRED_SLOTS[intent] - set(slots)))
        if intent == ASSET_INVENTORY_INTENT and not (_ASSET_SELECTOR_SLOTS & slots.keys()):
            missing = ("asset_selector",)
        return IntentDraft(
            intent=intent,
            slots={key: slots[key] for key in sorted(slots)},
            missing=missing,
            confidence=(
                self._RECOGNISED_CONFIDENCE if recognised else self._UNRECOGNISED_CONFIDENCE
            ),
            # 规则式解释器的输入是用户原文本身，没有模型参与。
            source=IntentSource.USER,
        )

    @classmethod
    def _recognise_intent(cls, text: str) -> str:
        matched = []
        if cls._is_slow_query(text):
            matched.append(SLOW_QUERY_INTENT)
        if cls._is_prometheus_alert_evidence(text):
            matched.append(PROMETHEUS_ALERT_INTENT)
        if cls._is_asset_inventory_lookup(text):
            matched.append(ASSET_INVENTORY_INTENT)
        return matched[0] if len(matched) == 1 else UNKNOWN_INTENT

    @staticmethod
    def _is_slow_query(text: str) -> bool:
        """慢查询问法的判定。

        刻意要求出现"慢"：匹配过宽会把导入失败、表结构、告警这类别的领域误路由
        到本能力上（near-miss 用例承重）。
        """
        compact = re.sub(r"\s+", "", text).lower()
        if "慢查询" in compact or "慢sql" in compact:
            return True
        return "慢" in compact and "查询" in compact

    @staticmethod
    def _is_prometheus_alert_evidence(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).lower()
        if any(marker in compact for marker in _PROMETHEUS_OTHER_SURFACES):
            return False
        if compact.startswith(_PROMETHEUS_WRITE_PREFIXES):
            return False
        return "告警" in compact and "证据" in compact

    @staticmethod
    def _is_asset_inventory_lookup(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).lower()
        if any(marker in compact for marker in _ASSET_EXCLUDED_MARKERS):
            return False
        subject = any(marker in compact for marker in ("资产", "机器", "主机"))
        lookup = any(marker in compact for marker in ("查", "查询", "lookup"))
        return subject and lookup

    def _extract_slots(self, text: str, *, intent: str) -> dict[str, str]:
        """逐个闭集槽位尝试填充；**先命中的规则优先**，后续规则不覆盖已填的槽位。"""
        if intent == UNKNOWN_INTENT:
            return {}
        if intent == SLOW_QUERY_INTENT:
            patterns = _SLOT_PATTERNS
        elif intent == PROMETHEUS_ALERT_INTENT:
            patterns = _PROMETHEUS_SLOT_PATTERNS
        else:
            patterns = _ASSET_SLOT_PATTERNS
        slots: dict[str, str] = {}
        for name, pattern in patterns:
            if name in slots:
                continue
            match = pattern.search(text)
            if match is not None:
                slots[name] = match.group(1)
        if intent in {SLOW_QUERY_INTENT, PROMETHEUS_ALERT_INTENT}:
            window = self._extract_window_minutes(text)
            if window is not None:
                slots["window_minutes"] = window
        allowed = self.SLOT_ALLOWLISTS[intent]
        return {key: value for key, value in slots.items() if key in allowed}

    @staticmethod
    def _extract_window_minutes(text: str) -> str | None:
        minutes = _WINDOW_MINUTES.search(text)
        if minutes is not None:
            return minutes.group(1)
        hours = _WINDOW_HOURS.search(text)
        if hours is not None:
            return str(int(hours.group(1)) * _MINUTES_PER_HOUR)
        return None
