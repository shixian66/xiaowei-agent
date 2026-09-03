"""``EvidenceLedger`` —— **PostgreSQL 绑定**。"""

from tests.suites.evidence_ledger import EVIDENCE_LEDGER_CASES, bind

bind(globals(), EVIDENCE_LEDGER_CASES)
