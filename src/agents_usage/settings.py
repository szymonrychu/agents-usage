"""USAGE_* settings of agents-usage.

A plain pydantic-settings class, not agents-common's BaseServiceSettings: that base demands an
orchestrator url and token, which the api and the prober never need. CsvList and SettingsError
are shared with the other services.
"""

import ipaddress

from agents_common.settings import CsvList, SettingsError
from pydantic import (
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = ["Settings", "SettingsError", "load_settings"]

PREFIX = "USAGE_"


class Settings(BaseSettings):
    """Environment-driven settings shared by every process (api, prober, watcher, migrate)."""

    model_config = SettingsConfigDict(env_prefix=PREFIX, extra="ignore")

    database_url: SecretStr = Field(repr=False)
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    service_port: int = 8001
    service_tokens_file: str | None = None
    ingest_port: int = 8002
    ingest_tokens_file: str | None = None
    allowed_cidrs: CsvList = ["127.0.0.1/32"]
    log_level: str = "INFO"
    shutdown_grace_seconds: float = 20
    stale_seconds: int = Field(1800, ge=60, le=86400)
    probe_after_seconds: int = Field(1800, ge=60, le=86400)
    probe_min_interval_seconds: int = Field(1800, ge=60, le=86400)
    probe_timeout_seconds: float = Field(120, ge=10, le=600)
    retention_days: int = Field(90, ge=1, le=3650)
    orchestrator_url: str | None = None
    orchestrator_token_file: str | None = None
    stream_read_timeout_seconds: float = Field(60, ge=10, le=600)
    reconnect_max_seconds: float = Field(60, ge=5, le=600)
    claude_bin: str = "claude"
    claude_model: str = "haiku"
    claude_home: str = "/tmp/claude-home"
    claude_token_file: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _split_csv(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields.get(info.field_name or "")
        if field is not None and NoDecode in field.metadata and isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("allowed_cidrs")
    @classmethod
    def _cidrs(cls, value: list[str], info: ValidationInfo) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(
                    f"{PREFIX}{str(info.field_name).upper()}::not a CIDR: {item!r}"
                ) from None
        return value

    @model_validator(mode="after")
    def _distinct_ports(self) -> "Settings":
        """Every enabled listener needs its own port."""
        taken = {"HTTP_PORT": self.http_port}
        for enabled, name, port in (
            (self.service_tokens_file, "SERVICE_PORT", self.service_port),
            (self.ingest_tokens_file, "INGEST_PORT", self.ingest_port),
        ):
            if not enabled:
                continue
            for other, other_port in taken.items():
                if port == other_port:
                    raise ValueError(f"{PREFIX}{name}::must differ from {PREFIX}{other}")
            taken[name] = port
        return self

    def api_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.allowed_cidrs]


def load_settings() -> Settings:
    """Load Settings from the environment or raise SettingsError naming every bad variable."""
    try:
        return Settings()
    except ValidationError as exc:
        problems: dict[str, str] = {}
        for err in exc.errors():
            msg = err["msg"].removeprefix("Value error, ")
            var, sep, rest = msg.partition("::")
            if sep and var.startswith(PREFIX):
                problems.setdefault(var, rest)
                continue
            msg = "required" if err["type"] == "missing" else msg
            loc = err["loc"]
            name = f"{PREFIX}{str(loc[0]).upper()}" if loc else f"{PREFIX}*"
            problems.setdefault(name, msg)
        raise SettingsError(problems) from None
