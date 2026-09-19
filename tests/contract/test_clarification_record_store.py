"""ClarificationRecordStore 内存绑定与协议形状。"""

from tests.suites.clarification_records import CLARIFICATION_RECORD_CASES, bind

from xiaowei_agent.persistence.clarification_records import ClarificationRecordStore

bind(globals(), CLARIFICATION_RECORD_CASES)


def test_clarification_record_store_has_only_two_narrow_methods() -> None:
    assert {name for name in dir(ClarificationRecordStore) if not name.startswith("_")} == {
        "load",
        "save",
    }
