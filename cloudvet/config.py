"""Explicit configuration; keys never enter the workspace archive."""

import os
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    agentbox_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    maas_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    maas_base_url: str = "https://api.gmi-serving.com/v1"
    default_idc: str = "us-central-iowa16"
    instance_type: str = "gmi.sandbox.small"
    agent_slug: str = ""
    image: str = "docker.io/library/python:3.12-bookworm"
    triage_model: str = "deepseek-ai/DeepSeek-V4.1-Flash"
    fix_model: str = "anthropic/claude-sonnet-4.5"
    timeout_seconds: int = Field(default=300, ge=60, le=3600)
    command_timeout: int = Field(default=120, ge=1, le=3500)

    @field_validator("maas_base_url")
    @classmethod
    def https_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("GMI_MAAS_BASE_URL must be an HTTPS URL without credentials")
        return value.rstrip("/")

    @classmethod
    def from_env(cls) -> "Settings":
        names = {
            "agentbox_api_key": "GMI_AGENTBOX_API_KEY",
            "maas_base_url": "GMI_MAAS_BASE_URL",
            "default_idc": "GMI_DEFAULT_IDC",
            "instance_type": "GMI_INSTANCE_TYPE",
            "agent_slug": "GMI_AGENT_SLUG",
            "image": "CLOUDVET_IMAGE",
            "triage_model": "GMI_TRIAGE_MODEL",
            "fix_model": "GMI_FIX_MODEL",
            "timeout_seconds": "CLOUDVET_TIMEOUT_SECONDS",
            "command_timeout": "CLOUDVET_COMMAND_TIMEOUT",
        }
        values = {field: os.environ[name] for field, name in names.items() if name in os.environ}
        values["maas_api_key"] = os.getenv("GMI_MAAS_API_KEY") or os.getenv("GMI_API_KEY", "")
        return cls(**values)

    def require_compute(self) -> None:
        if not self.agentbox_api_key.get_secret_value():
            raise ValueError("Set GMI_AGENTBOX_API_KEY to a compute-scoped GMI key")

    def require_inference(self) -> None:
        if not self.maas_api_key.get_secret_value():
            raise ValueError("Set GMI_MAAS_API_KEY or GMI_API_KEY for GMI inference")
