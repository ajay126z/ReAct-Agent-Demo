from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from react_mcp.agent.memory import (
    ConversationMemory,
    ConversationTurn,
    SessionStore,
)


def turn(question: str, answer: str = "answer") -> ConversationTurn:
    return ConversationTurn.create(question, answer, model="test-model")


def test_memory_trims_by_turn_count() -> None:
    memory = ConversationMemory(max_turns=2, max_chars=1000)
    for index in range(3):
        memory.add(turn(f"q{index}"))

    assert [item.question for item in memory.turns] == ["q1", "q2"]


def test_memory_trims_by_character_count() -> None:
    memory = ConversationMemory(max_turns=10, max_chars=8)
    memory.add(turn("aa", "bb"))
    memory.add(turn("cc", "dd"))
    memory.add(turn("ee", "ff"))

    assert [item.question for item in memory.turns] == ["cc", "ee"]


def test_memory_can_clear() -> None:
    memory = ConversationMemory(max_turns=2, max_chars=100)
    memory.extend([turn("one"), turn("two")])
    memory.clear()

    assert len(memory) == 0
    assert memory.turns == []


def test_store_append_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    original = turn("What is 2+2?", "4")

    store.append("demo", original)
    loaded = store.load("demo", max_turns=6, max_chars=1000)

    assert store.exists("demo")
    assert loaded.turns == [original]


def test_store_load_missing_session_is_empty(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")

    loaded = store.load("missing", max_turns=6, max_chars=1000)

    assert loaded.turns == []


def test_store_skips_corrupt_lines(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    original = turn("kept", "yes")
    store.append("demo", original)
    with store.path("demo").open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"question": "missing answer"}) + "\n")

    loaded = store.load("demo", max_turns=6, max_chars=1000)

    assert loaded.turns == [original]


def test_store_reset_archives_existing_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.append("demo", turn("old", "answer"))

    archived = store.reset("demo")

    assert archived is not None
    assert archived.is_file()
    assert archived.parent == tmp_path / "sessions" / "archive"
    assert not store.exists("demo")
    assert store.load("demo", max_turns=6, max_chars=1000).turns == []


def test_store_reset_missing_session_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")

    assert store.reset("missing") is None


def test_store_delete_removes_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.append("demo", turn("question"))

    assert store.delete("demo") is True
    assert store.delete("demo") is False
    assert not store.exists("demo")


def test_store_latest_uses_modification_time(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.append("older", turn("old"))
    store.append("newer", turn("new"))
    now = time.time()
    os.utime(store.path("older"), (now - 10, now - 10))
    os.utime(store.path("newer"), (now, now))

    assert store.latest() == "newer"


def test_store_list_sessions_counts_turns(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.append("demo", turn("one"))
    store.append("demo", turn("two"))

    sessions = store.list_sessions()

    assert [session.name for session in sessions] == ["demo"]
    assert sessions[0].turns == 2


@pytest.mark.parametrize("name", ["", "../escape", "bad/name", ".hidden", "x" * 65])
def test_store_rejects_unsafe_session_names(name: str) -> None:
    with pytest.raises(ValueError):
        SessionStore.validate_name(name)


def test_store_accepts_human_readable_names() -> None:
    assert SessionStore.validate_name("my-session_01.v2") == "my-session_01.v2"
