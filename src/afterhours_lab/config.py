"""Environment configuration for the gateway connection. Fails closed."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """AfterHoursLab talks to SchwabGateway only; there is no direct-access mode."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gateway_url: str = Field(validation_alias="SCHWAB_GATEWAY_URL")
    gateway_api_key: SecretStr = Field(validation_alias="SCHWAB_GATEWAY_API_KEY")
    gateway_timeout_seconds: float = Field(
        default=5.0, validation_alias="SCHWAB_GATEWAY_TIMEOUT_SECONDS"
    )
    gateway_max_concurrency: int = Field(
        default=4, validation_alias="SCHWAB_GATEWAY_MAX_CONCURRENCY"
    )
    gateway_max_attempts: int = Field(
        default=3, validation_alias="SCHWAB_GATEWAY_MAX_ATTEMPTS"
    )
    gateway_retry_backoff_seconds: float = Field(
        default=0.5, validation_alias="SCHWAB_GATEWAY_RETRY_BACKOFF_SECONDS"
    )

    @field_validator("gateway_url")
    @classmethod
    def gateway_url_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SCHWAB_GATEWAY_URL must not be blank")
        return value

    @field_validator("gateway_api_key")
    @classmethod
    def gateway_api_key_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("SCHWAB_GATEWAY_API_KEY must not be blank")
        return value

    @field_validator("gateway_timeout_seconds")
    @classmethod
    def gateway_timeout_must_be_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("SCHWAB_GATEWAY_TIMEOUT_SECONDS must be positive")
        return value

    @field_validator("gateway_max_concurrency")
    @classmethod
    def gateway_concurrency_must_be_bounded(cls, value: int) -> int:
        if not 1 <= value <= 32:
            raise ValueError("SCHWAB_GATEWAY_MAX_CONCURRENCY must be between 1 and 32")
        return value

    @field_validator("gateway_max_attempts")
    @classmethod
    def gateway_attempts_must_be_bounded(cls, value: int) -> int:
        if not 1 <= value <= 6:
            raise ValueError("SCHWAB_GATEWAY_MAX_ATTEMPTS must be between 1 and 6")
        return value

    @field_validator("gateway_retry_backoff_seconds")
    @classmethod
    def gateway_backoff_must_be_nonnegative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("SCHWAB_GATEWAY_RETRY_BACKOFF_SECONDS must be nonnegative")
        return value
