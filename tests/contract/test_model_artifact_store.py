"""ModelArtifactStore 内存绑定与协议形状。"""

from tests.suites.model_artifacts import MODEL_ARTIFACT_CASES, bind

from xiaowei_agent.persistence.model_artifacts import ModelArtifactStore

bind(globals(), MODEL_ARTIFACT_CASES)


def test_model_artifact_store_has_only_four_narrow_methods() -> None:
    assert {name for name in dir(ModelArtifactStore) if not name.startswith("_")} == {
        "load_intent",
        "save_intent",
        "load_advisory",
        "save_advisory",
    }
