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
    assert env["REACT_MCP_REQUIRE_WRITE_APPROVAL"] == "true"


def test_memory_defaults_and_session_dir(settings: Settings) -> None:
    settings.ensure_dirs()
    assert settings.memory_turns == 6
    assert settings.memory_chars == 4000
    assert settings.session_dir.is_dir()


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


def test_cli_parses_session_flags() -> None:
    ask = build_parser().parse_args(["ask", "hello", "--session", "demo"])
    assert ask.session == "demo"
    assert ask.continue_session is False

    repl = build_parser().parse_args(["repl", "--continue"])
    assert repl.session is None
    assert repl.continue_session is True


def test_cli_parses_protocol_feature_flags() -> None:
    args = build_parser().parse_args(
        ["ask", "hello", "--progress", "--server-logs", "--auto-approve-writes"]
    )
    assert args.progress is True
    assert args.server_logs is True
    assert args.auto_approve_writes is True


def test_cli_parses_session_management() -> None:
    listed = build_parser().parse_args(["session", "list", "--quiet"])
    assert listed.command == "session"
    assert listed.session_command == "list"
    assert listed.quiet is True

    deleted = build_parser().parse_args(["session", "delete", "demo"])
    assert deleted.session_command == "delete"
    assert deleted.session_name == "demo"
