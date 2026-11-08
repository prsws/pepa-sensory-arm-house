"""A new setup starts local: no cloud endpoint or cloud model by default."""

from custom_components.pepa_sensory_arm_house import const


def test_main_model_defaults_are_local():
    assert const.DEFAULT_LLM_BASE_URL == "http://localhost:11434/v1"
    assert const.DEFAULT_LLM_MODEL == "gemma4:e2b"
    assert const.DEFAULT_TEMPERATURE == 0.0
    assert const.DEFAULT_LLM_KEEP_ALIVE == "15m"


def test_no_cloud_default_for_the_main_model():
    assert "openai.com" not in const.DEFAULT_LLM_BASE_URL
    assert not const.DEFAULT_LLM_MODEL.startswith("gpt-")
