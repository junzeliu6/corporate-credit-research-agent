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


class AuditedOpenAIChatCompletionsModel(OpenAIChatCompletionsModel):
    """在SDK归一化响应前保存非敏感的 ``finish_reason``。

    Agents SDK 的 ``ModelResponse`` 当前不稳定暴露供应商原始
    ``finish_reason``。本子类仅截获 Chat Completions 元数据，不记录请求、
    API Key 或模型正文；网络和响应解析仍完全复用 SDK 的实现。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.finish_reasons: list[Optional[str]] = []

    async def _fetch_response(self, *args: Any, **kwargs: Any) -> Any:
        response = await super()._fetch_response(*args, **kwargs)
        finish_reason: Optional[str] = None
        # 当前 Agent 使用非流式 Runner.run，返回对象是 ChatCompletion。
        # 对未来的流式元组保持安全降级，不猜测尚未结束的 finish_reason。
        raw_response = response[0] if isinstance(response, tuple) else response
        choices = getattr(raw_response, "choices", None)
        if choices:
            raw_reason = getattr(choices[0], "finish_reason", None)
            finish_reason = str(raw_reason) if raw_reason is not None else None
        self.finish_reasons.append(finish_reason)
        return response


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
    max_turns: int = Field(default=4, ge=2, le=70)
    max_tool_calls: int = Field(default=1, ge=1, le=60)
    # 该值限制每次模型响应的输出，不是整个Agent Loop的输入+输出总预算。
    max_output_tokens: int = Field(default=800, ge=128, le=50000)
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


def validate_http_transport_compatibility() -> None:
    """在发送 Live 请求前验证 HTTP 客户端的 Brotli 解压接口。

    ``httpx2`` 会在可导入 Brotli 时声明支持 ``br`` 响应。项目虚拟环境若
    继承了旧版系统包，响应可能已到达本机，却在读取正文时才因接口不兼容而
    失败。这里用本地字节做一次压缩/解压往返，不访问网络，也不接触 API Key。
    """

    try:
        from httpx2 import _decoders
    except (ImportError, ModuleNotFoundError) as exc:
        raise LLMConfigError(
            "http_transport_unavailable",
            "OpenAI客户端依赖的httpx2解码模块不可用，请重新安装项目依赖",
        ) from exc

    if _decoders.brotli is None:
        # 未安装可选Brotli时，httpx2不会声明br编码；无需额外校验。
        return

    payload = b"corporate-credit-research-agent-brotli-preflight"
    try:
        encoded = _decoders.brotli.compress(payload)
        decoder = _decoders.BrotliDecoder()
        decoded = b"".join(decoder.decode(encoded)) + b"".join(decoder.flush())
    except TypeError as exc:
        raise LLMConfigError(
            "incompatible_brotli_decoder",
            "当前Brotli解压接口与httpx2不兼容；CPython需要Brotli>=1.2",
        ) from exc
    except Exception as exc:
        raise LLMConfigError(
            "http_transport_preflight_failed",
            f"HTTP响应解压预检失败：{type(exc).__name__}",
        ) from exc

    if decoded != payload:
        raise LLMConfigError(
            "http_transport_preflight_failed",
            "HTTP响应解压预检结果不一致",
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
    validate_http_transport_compatibility()
    client = AsyncOpenAI(
        api_key=config.api_key.get_secret_value(),
        base_url=config.base_url,
        timeout=config.request_timeout_seconds,
        # 重试由 Agents SDK 的有限策略统一控制，避免叠加重试。
        max_retries=0,
    )
    model = AuditedOpenAIChatCompletionsModel(
        model=config.model,
        openai_client=client,
        # 当前关闭 thinking，不启用 reasoning_content 多轮回放。
        should_replay_reasoning_content=False,
    )
    return model, client
