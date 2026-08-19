"""Environment configuration for the gateway connection. Fails closed."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """AfterHoursLab talks to SchwabGateway only; there is no direct-access mode."""

    model_config = SettingsConfigDict(extra="ignore")

    gateway_url: str = Field(validation_alias="SCHWAB_GATEWAY_URL")
    gateway_api_key: SecretStr = Field(validation_alias="SCHWAB_GATEWAY_API_KEY")

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
