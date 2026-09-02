"""证据构造。**本包是纯构造器**：无 async、无 I/O、只依赖 contracts。

这条纯度是 ``runners → evidence`` 依赖边的对价，由
``tests/security/test_evidence_layer_purity.py`` 承重。
"""

from xiaowei_agent.evidence.builder import build_evidence, evidence_id

__all__ = ["build_evidence", "evidence_id"]
