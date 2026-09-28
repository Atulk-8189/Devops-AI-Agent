"""Validated, non-secret runtime configuration."""

from dataclasses import dataclass
import os
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


DEFAULT_REQUEST_DEADLINE_SECONDS = 300
DEFAULT_KUBECTL_LOGS_MAX_CHARS = 256_000


@dataclass(frozen=True, repr=False)
class Settings:
    azure_openai_endpoint: str
    azure_openai_api_key: str
    aks_mcp_path: str
    azure_config_dir: str | None = None
    model: str = "gpt-5-mini"
    request_deadline_seconds: int = DEFAULT_REQUEST_DEADLINE_SECONDS
    kubectl_logs_max_chars: int = DEFAULT_KUBECTL_LOGS_MAX_CHARS

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
    logs_raw = values.get("KUBECTL_LOGS_MAX_CHARS") or values.get("CALL_KUBECTL_MAX_CHARS")
    if logs_raw is not None:
        try:
            logs_limit = int(logs_raw)
            if not 1000 <= logs_limit <= 4_000_000:
                raise ValueError
        except (ValueError, TypeError):
            raise ConfigurationError("Invalid kubectl logs max chars configuration") from None
    else:
        logs_limit = DEFAULT_KUBECTL_LOGS_MAX_CHARS
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
        kubectl_logs_max_chars=logs_limit,
    )
