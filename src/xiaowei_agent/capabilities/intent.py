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
from typing import Final, Protocol

from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext

SLOW_QUERY_INTENT: Final[str] = "starrocks.slow_query.diagnose"
UNKNOWN_INTENT: Final[str] = "unknown"

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

_WINDOW_MINUTES: Final[re.Pattern[str]] = re.compile(
    r"(?:window_minutes\s*=\s*|最近\s*)(\d{1,5})\s*分钟"
)
_WINDOW_HOURS: Final[re.Pattern[str]] = re.compile(r"最近\s*(\d{1,3})\s*小时")

_MINUTES_PER_HOUR: Final[int] = 60


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

    ALLOWED_SLOTS: Final[frozenset[str]] = frozenset(
        {"environment_id", "database", "user_name", "query_id", "window_minutes"}
    )
    """槽位闭集。

    新增槽位必须显式改这个集合并过评审（``test_interpreter_slot_allowlist_is_closed``
    反向承重）。``sql`` / ``operation`` / ``capability_id`` / ``effect_class`` /
    ``side_effect`` / ``policy_profile`` / ``approval_ref`` 这类执行权字段永远
    不在其中。
    """

    REQUIRED_SLOTS: Final[frozenset[str]] = frozenset({"environment_id"})
    """必须被填上的槽位；填不上即进 ``missing``，由 Runtime 决定是否向用户追问。"""

    _RECOGNISED_CONFIDENCE: Final[float] = 0.9
    _UNRECOGNISED_CONFIDENCE: Final[float] = 0.0

    def interpret(self, *, text: str, context: RequestContext) -> IntentDraft:
        """按闭集规则产出草案。

        :param text: 用户原文，按不可信外部文本处理。
        :param context: 执行上下文；``environment_id`` 在原文未指定时由它兜底。
        """
        slots = self._extract_slots(text)
        slots.setdefault("environment_id", context.environment_id)
        recognised = self._is_slow_query(text)
        return IntentDraft(
            intent=SLOW_QUERY_INTENT if recognised else UNKNOWN_INTENT,
            slots={key: slots[key] for key in sorted(slots)},
            missing=tuple(sorted(self.REQUIRED_SLOTS - set(slots))),
            confidence=(
                self._RECOGNISED_CONFIDENCE if recognised else self._UNRECOGNISED_CONFIDENCE
            ),
            # 规则式解释器的输入是用户原文本身，没有模型参与。
            source=IntentSource.USER,
        )

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

    def _extract_slots(self, text: str) -> dict[str, str]:
        """逐个闭集槽位尝试填充；**先命中的规则优先**，后续规则不覆盖已填的槽位。"""
        slots: dict[str, str] = {}
        for name, pattern in _SLOT_PATTERNS:
            if name in slots:
                continue
            match = pattern.search(text)
            if match is not None:
                slots[name] = match.group(1)
        window = self._extract_window_minutes(text)
        if window is not None:
            slots["window_minutes"] = window
        return {key: value for key, value in slots.items() if key in self.ALLOWED_SLOTS}

    @staticmethod
    def _extract_window_minutes(text: str) -> str | None:
        minutes = _WINDOW_MINUTES.search(text)
        if minutes is not None:
            return minutes.group(1)
        hours = _WINDOW_HOURS.search(text)
        if hours is not None:
            return str(int(hours.group(1)) * _MINUTES_PER_HOUR)
        return None
