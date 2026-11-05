"""Configuration for the agent, the MCP client, and the MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE = PROJECT_ROOT / "data" / "workspace"
DEFAULT_NOTES = PROJECT_ROOT / "data" / "notes"
DEFAULT_SESSIONS = PROJECT_ROOT / "data" / "sessions"
DEFAULT_TRACES = PROJECT_ROOT / "traces"


class Settings(BaseSettings):
    """Environment-driven settings with the ``REACT_MCP_`` prefix."""

    model_config = SettingsConfigDict(
        env_prefix="REACT_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Model
    model: str = "qwen3.5:9b"
    ollama_host: str = "http://localhost:11434"
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    num_ctx: int = Field(default=8192, ge=512)
    keep_alive: str = "10m"
    think: bool = False
    request_timeout: float = Field(default=180.0, gt=0)

    # Agent
    max_steps: int = Field(default=8, ge=1, le=50)
    max_parse_retries: int = Field(default=2, ge=0, le=10)
    max_observation_chars: int = Field(default=2000, ge=100)
    memory_turns: int = Field(default=6, ge=1, le=50)
    memory_chars: int = Field(default=4000, ge=100, le=100_000)
    require_write_approval: bool = True

    # MCP transport
    transport: Literal["stdio", "http"] = "stdio"
    server_url: str = "http://127.0.0.1:8000/mcp"

    # Web (OpenCode-style free fetch + search)
    web_fetch_enabled: bool = True
    web_fetch_timeout: float = Field(default=15.0, gt=0, le=60)
    web_fetch_max_bytes: int = Field(default=500_000, ge=1000, le=5_000_000)
    web_fetch_max_chars: int = Field(default=20_000, ge=500, le=100_000)
    web_search_limit: int = Field(default=10, ge=1, le=20)
    web_user_agent: str = "react-mcp/0.1 (+learning project)"

    # Paths
    workspace_dir: Path = DEFAULT_WORKSPACE
    notes_dir: Path = DEFAULT_NOTES
    session_dir: Path = DEFAULT_SESSIONS
    trace_dir: Path = DEFAULT_TRACES

    # Observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    otel: Literal["none", "console", "otlp"] = "none"
    otlp_endpoint: str = "http://localhost:4317"

    @field_validator("workspace_dir", "notes_dir", "session_dir", "trace_dir")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (Path.cwd() / value).resolve()

    def ensure_dirs(self) -> None:
        """Create the directories the server and CLI write to."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    def server_env(self) -> dict[str, str]:
        """Environment passed to the stdio MCP server subprocess.

        ``StdioServerParameters`` gives the child an allow-listed environment,
        so every setting the server needs has to be explicit here.
        """
        return {
            "REACT_MCP_WORKSPACE_DIR": str(self.workspace_dir),
            "REACT_MCP_NOTES_DIR": str(self.notes_dir),
            "REACT_MCP_TRACE_DIR": str(self.trace_dir),
            "REACT_MCP_REQUIRE_WRITE_APPROVAL": str(self.require_write_approval).lower(),
            "REACT_MCP_LOG_LEVEL": self.log_level,
            "REACT_MCP_OTEL": self.otel,
            "REACT_MCP_OTLP_ENDPOINT": self.otlp_endpoint,
            "REACT_MCP_WEB_FETCH_ENABLED": str(self.web_fetch_enabled).lower(),
            "REACT_MCP_WEB_FETCH_TIMEOUT": str(self.web_fetch_timeout),
            "REACT_MCP_WEB_FETCH_MAX_BYTES": str(self.web_fetch_max_bytes),
            "REACT_MCP_WEB_FETCH_MAX_CHARS": str(self.web_fetch_max_chars),
            "REACT_MCP_WEB_SEARCH_LIMIT": str(self.web_search_limit),
            "REACT_MCP_WEB_USER_AGENT": self.web_user_agent,
        }

    def redacted(self) -> dict[str, object]:
        """Settings safe to expose as an MCP resource."""
        data = self.model_dump(mode="json")
        return data


def load_settings(**overrides: object) -> Settings:
    """Load settings from the environment, then apply non-None overrides."""
    settings = Settings()
    clean = {key: value for key, value in overrides.items() if value is not None}
    return settings.model_copy(update=clean) if clean else settings
