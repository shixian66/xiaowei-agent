"""PR 3C 只依赖 application 模型失败契约，不认识 Gemini adapter。"""

from xiaowei_agent.application import model_ports


def test_intent_retryable_error_codes_are_the_exact_provider_neutral_set() -> None:
    codes = getattr(model_ports, "INTENT_RETRYABLE_ERROR_CODES", None)

    assert codes is not None
    assert {code.value for code in codes} == {
        "model_rate_limited",
        "model_server_error",
        "model_transport_error",
    }


def test_model_port_error_has_only_a_safe_closed_code_message() -> None:
    error_type = getattr(model_ports, "ModelPortError", None)

    assert error_type is not None
    code = next(iter(model_ports.INTENT_RETRYABLE_ERROR_CODES))
    error = error_type(code)
    assert error.code is code
    assert str(error) == code.value
    assert error.__cause__ is None
    assert error.__context__ is None
