"""V1.3B LLM Provider 配置。

业务代码只依赖本模块返回的配置与模型，不直接读取密钥或硬编码供应商。
默认 Mock 模式不会创建网络客户端；Live 模式必须显式选择并提供密钥。
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Tuple

from agents import (
    ModelRetryBackoffSettings,
    ModelRetrySettings,
    ModelSettings,
    OpenAIChatCompletionsModel,
    retry_policies,
)
from dotenv import dotenv_values
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"


class AgentMode(str, Enum):
    MOCK = "mock"
    LIVE = "live"


class LLMConfigError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeepSeekLLMConfig(BaseModel):
    """可安全记录的 DeepSeek 运行配置。

    ``api_key`` 使用 SecretStr、禁止 repr，并从序列化中排除。日志只能使用
    :meth:`sanitized_summary`，不得直接 dump 整个对象。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: AgentMode = AgentMode.MOCK
    provider: Literal["deepseek"] = DEEPSEEK_PROVIDER
    base_url: str = DEEPSEEK_BASE_URL
    model: str = DEEPSEEK_DEFAULT_MODEL
    api_key_env: Literal["DEEPSEEK_API_KEY"] = DEEPSEEK_API_KEY_ENV
    api_key: Optional[SecretStr] = Field(default=None, exclude=True, repr=False)
    thinking: Literal["disabled"] = "disabled"
    max_turns: int = Field(default=4, ge=2, le=8)
    max_tool_calls: int = Field(default=1, ge=1, le=3)
    max_output_tokens: int = Field(default=800, ge=128, le=4096)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_retries: int = Field(default=1, ge=0, le=2)

    @model_validator(mode="after")
    def validate_live_requirements(self) -> "DeepSeekLLMConfig":
        if not self.base_url.startswith("https://"):
            raise ValueError("DeepSeek Base URL 必须使用 HTTPS")
        if self.mode is AgentMode.LIVE and self.api_key is None:
            raise ValueError(
                f"Live 模式缺少环境变量 {self.api_key_env}；Mock 模式不需要密钥"
            )
        return self

    def sanitized_summary(self) -> Dict[str, Any]:
        """只返回允许显示或写入执行轨迹的字段。"""

        return {
            "mode": self.mode.value,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_configured": self.api_key is not None,
            "thinking": self.thinking,
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_output_tokens": self.max_output_tokens,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
        }


def load_deepseek_config(
    project_root: Path,
    *,
    mode: AgentMode | str = AgentMode.MOCK,
    env_file: Optional[Path] = None,
    **overrides: Any,
) -> DeepSeekLLMConfig:
    """从环境变量或项目本地 ``.env`` 读取密钥。

    系统环境变量优先于 ``.env``。Mock 模式故意不读取密钥，既避免不必要地把
    敏感值带入进程对象，也能证明离线测试不依赖密钥。
    """

    resolved_mode = AgentMode(mode)
    api_key: Optional[SecretStr] = None
    if resolved_mode is AgentMode.LIVE:
        file_path = Path(env_file) if env_file else Path(project_root) / ".env"
        file_values = dotenv_values(file_path) if file_path.is_file() else {}
        raw_key = os.environ.get(DEEPSEEK_API_KEY_ENV) or file_values.get(
            DEEPSEEK_API_KEY_ENV
        )
        if raw_key:
            api_key = SecretStr(str(raw_key).strip())
        else:
            raise LLMConfigError(
                "missing_api_key",
                f"Live 模式需要环境变量或 .env 中的 {DEEPSEEK_API_KEY_ENV}",
            )
    try:
        return DeepSeekLLMConfig(
            mode=resolved_mode,
            api_key=api_key,
            **overrides,
        )
    except ValueError as exc:
        raise LLMConfigError("invalid_llm_config", str(exc)) from exc


def build_model_settings(config: DeepSeekLLMConfig) -> ModelSettings:
    """构造保守的模型参数；首版固定关闭 DeepSeek thinking。"""

    retry_policy = retry_policies.any(
        retry_policies.network_error(),
        retry_policies.http_status({408, 409, 429, 500, 502, 503, 504}),
    )
    return ModelSettings(
        parallel_tool_calls=False,
        max_tokens=config.max_output_tokens,
        include_usage=True,
        preserve_raw_usage=True,
        timeout=config.request_timeout_seconds,
        retry=ModelRetrySettings(
            max_retries=config.max_retries,
            backoff=ModelRetryBackoffSettings(
                initial_delay=0.5,
                max_delay=2.0,
                multiplier=2.0,
                jitter=True,
            ),
            policy=retry_policy,
        ),
        # DeepSeek 官方 OpenAI 兼容接口通过 extra_body 接收 thinking 开关。
        extra_body={"thinking": {"type": config.thinking}},
    )


def build_live_deepseek_model(
    config: DeepSeekLLMConfig,
) -> Tuple[OpenAIChatCompletionsModel, AsyncOpenAI]:
    """创建 DeepSeek Chat Completions 模型与需由调用方关闭的客户端。"""

    if config.mode is not AgentMode.LIVE:
        raise LLMConfigError(
            "live_model_in_mock_mode", "Mock 配置不得创建真实网络客户端"
        )
    if config.api_key is None:  # 双重保护，避免未来构造方式绕过校验。
        raise LLMConfigError("missing_api_key", "Live 模式缺少 DeepSeek API Key")
    client = AsyncOpenAI(
        api_key=config.api_key.get_secret_value(),
        base_url=config.base_url,
        timeout=config.request_timeout_seconds,
        # 重试由 Agents SDK 的有限策略统一控制，避免叠加重试。
        max_retries=0,
    )
    model = OpenAIChatCompletionsModel(
        model=config.model,
        openai_client=client,
        # 当前关闭 thinking，不启用 reasoning_content 多轮回放。
        should_replay_reasoning_content=False,
    )
    return model, client
