"""``EvidenceLedger`` —— **内存绑定** + 与实现无关的契约断言。

行为用例定义在 ``tests/suites/evidence_ledger.py``，PostgreSQL 绑定在
``tests/integration/``。
"""

from tests.suites.evidence_ledger import EVIDENCE_LEDGER_CASES, bind

from xiaowei_agent.persistence.evidence import EvidenceLedger

bind(globals(), EVIDENCE_LEDGER_CASES)


def test_ledger_protocol_surface_is_exactly_three_methods() -> None:
    assert {m for m in dir(EvidenceLedger) if not m.startswith("_")} == {
        "append",
        "load",
        "get",
    }
