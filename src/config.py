"""Validated, non-secret runtime configuration."""

from dataclasses import dataclass
import os
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


DEFAULT_REQUEST_DEADLINE_SECONDS = 300


@dataclass(frozen=True, repr=False)
class Settings:
    azure_openai_endpoint: str
    azure_openai_api_key: str
    aks_mcp_path: str
    azure_config_dir: str | None = None
    model: str = "gpt-5-mini"
    request_deadline_seconds: int = DEFAULT_REQUEST_DEADLINE_SECONDS

    def safe_summary(self):
        return {"endpoint_configured": bool(self.azure_openai_endpoint),
                "credentials_configured": bool(self.azure_openai_api_key),
                "aks_configured": bool(self.aks_mcp_path)}

    def __repr__(self):
        return f"Settings({self.safe_summary()!r})"


def load_settings(env=None) -> Settings:
    values = os.environ if env is None else env
    try:
        deadline = int(values.get("REQUEST_DEADLINE_SECONDS", DEFAULT_REQUEST_DEADLINE_SECONDS))
        if not 1 <= deadline <= 1800:
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError("Invalid request deadline configuration") from None
    missing = [name for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AKS_MCP_PATH")
               if not values.get(name, "").strip()]
    if missing:
        raise ConfigurationError("Missing required configuration: " + ", ".join(missing))
    endpoint = values["AZURE_OPENAI_ENDPOINT"].strip().rstrip("/")
    if not endpoint.startswith(("https://", "http://")):
        raise ConfigurationError("AZURE_OPENAI_ENDPOINT must be an HTTP(S) endpoint")
    return Settings(
        azure_openai_endpoint=endpoint,
        azure_openai_api_key=values["AZURE_OPENAI_API_KEY"],
        aks_mcp_path=str(Path(values["AKS_MCP_PATH"].strip()).expanduser()),
        azure_config_dir=values.get("AZURE_CONFIG_DIR") or None,
        request_deadline_seconds=deadline,
    )
