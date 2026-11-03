from __future__ import annotations

from react_mcp.cli import build_parser
from react_mcp.config import Settings, load_settings


def test_load_settings_applies_non_none_overrides(monkeypatch) -> None:
    monkeypatch.setenv("REACT_MCP_MODEL", "env-model")
    monkeypatch.setenv("REACT_MCP_MAX_STEPS", "5")
    settings = load_settings(model="override-model", max_steps=None, temperature=0.4)
    assert settings.model == "override-model"
    assert settings.max_steps == 5
    assert settings.temperature == 0.4


def test_server_env_carries_paths_and_observability(settings: Settings) -> None:
    env = settings.server_env()
    assert env["REACT_MCP_WORKSPACE_DIR"] == str(settings.workspace_dir)
    assert env["REACT_MCP_NOTES_DIR"] == str(settings.notes_dir)
    assert env["REACT_MCP_LOG_LEVEL"] == "INFO"
    assert env["REACT_MCP_OTEL"] == "none"


def test_redacted_dump_is_json_safe(settings: Settings) -> None:
    data = settings.redacted()
    assert data["model"] == "test-model"
    assert isinstance(data["workspace_dir"], str)


def test_cli_parses_ask_and_flags() -> None:
    args = build_parser().parse_args(
        ["ask", "what", "is", "2+2", "--no-trace", "--quiet", "--max-steps", "3"]
    )
    assert args.command == "ask"
    assert args.question == ["what", "is", "2+2"]
    assert args.no_trace is True
    assert args.quiet is True
    assert args.max_steps == 3


def test_cli_parses_serve_http() -> None:
    args = build_parser().parse_args(["serve", "--http", "--port", "9000"])
    assert args.command == "serve"
    assert args.http is True
    assert args.port == 9000
