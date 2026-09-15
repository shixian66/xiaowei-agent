"""PR 3C 只依赖 application 模型失败契约，不认识 Gemini adapter。"""

from xiaowei_agent.application import model_ports


def test_model_error_fallback_mapping_is_total_over_provider_neutral_codes() -> None:
    codes = getattr(model_ports, "MODEL_ERROR_FALLBACK_CODES", None)

    assert codes is not None
    from xiaowei_agent.contracts import ModelErrorCode, ModelFallbackCode

    assert set(codes) == set(ModelErrorCode)
    assert set(codes.values()) <= set(ModelFallbackCode)


def test_model_port_error_has_only_a_safe_closed_code_message() -> None:
    error_type = getattr(model_ports, "ModelPortError", None)

    assert error_type is not None
    code = next(iter(model_ports.MODEL_ERROR_FALLBACK_CODES))
    error = error_type(code)
    assert error.code is code
    assert str(error) == code.value
    assert error.__cause__ is None
    assert error.__context__ is None
