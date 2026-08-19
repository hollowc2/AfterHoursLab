"""Connection settings for AfterHoursLab's own isolated database.

A separate database on the shared TimescaleDB instance (same convention Butterflyguy
uses: one database per app/strategy, same host, different DATABASE__NAME). Never shares
credentials with another app's database.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = Field(validation_alias="DATABASE__HOST")
    port: int = Field(default=5432, validation_alias="DATABASE__PORT")
    name: str = Field(validation_alias="DATABASE__NAME")
    user: str = Field(validation_alias="DATABASE__USER")
    password: SecretStr = Field(validation_alias="DATABASE__PASSWORD")

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )
