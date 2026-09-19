"""ClarificationRecordStore PostgreSQL 共享绑定。"""

from tests.suites.clarification_records import CLARIFICATION_RECORD_CASES, bind

bind(globals(), CLARIFICATION_RECORD_CASES)
