"""ModelArtifactStore PostgreSQL 共享绑定。"""

from tests.suites.model_artifacts import MODEL_ARTIFACT_CASES, bind

bind(globals(), MODEL_ARTIFACT_CASES)
