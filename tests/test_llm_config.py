"""V1.3B DeepSeek配置与密钥安全离线测试。"""

import asyncio
from pathlib import Path

import pytest

from credit_research_agent.llm_config import (
    AgentMode,
    LLMConfigError,
    build_live_deepseek_model,
    build_model_settings,
    load_deepseek_config,
)


def test_default_mock_config_needs_no_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = load_deepseek_config(tmp_path)

    assert config.mode is AgentMode.MOCK
    assert config.provider == "deepseek"
    assert config.base_url == "https://api.deepseek.com"
    assert config.model == "deepseek-flash"
    assert config.api_key is None
    assert config.sanitized_summary()["api_key_configured"] is False


def test_live_config_requires_key_without_leaking_value(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(LLMConfigError) as error:
        load_deepseek_config(tmp_path, mode="live")

    assert error.value.code == "missing_api_key"
    assert "DEEPSEEK_API_KEY" in str(error.value)


def test_env_file_secret_is_excluded_from_summary_and_serialization(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "sk-test-never-log-this-value"
    (tmp_path / ".env").write_text(
        f"DEEPSEEK_API_KEY={secret}\n", encoding="utf-8"
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = load_deepseek_config(tmp_path, mode="live")
    summary = str(config.sanitized_summary())
    serialized = config.model_dump_json()

    assert config.api_key is not None
    assert secret not in repr(config)
    assert secret not in summary
    assert secret not in serialized
    assert config.sanitized_summary()["api_key_configured"] is True


def test_model_settings_disable_thinking_and_apply_limits(tmp_path: Path) -> None:
    config = load_deepseek_config(
        tmp_path,
        max_output_tokens=512,
        max_retries=1,
        request_timeout_seconds=15,
    )

    settings = build_model_settings(config)

    assert settings.extra_body == {"thinking": {"type": "disabled"}}
    assert settings.parallel_tool_calls is False
    assert settings.max_tokens == 512
    assert settings.timeout == 15
    assert settings.retry is not None
    assert settings.retry.max_retries == 1


def test_live_model_uses_deepseek_chat_completions_without_request(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-construction-only")
    config = load_deepseek_config(tmp_path, mode="live")

    model, client = build_live_deepseek_model(config)
    try:
        assert model.model == "deepseek-flash"
        assert str(client.base_url).rstrip("/") == "https://api.deepseek.com"
    finally:
        asyncio.run(client.close())
