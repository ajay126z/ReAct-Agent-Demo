from __future__ import annotations

from react_mcp.agent.parser import parse_action_input, parse_react_output


def test_parses_final_answer_with_thought() -> None:
    parsed = parse_react_output("Thought: I have the result.\nFinal Answer: 60")
    assert parsed.kind == "final"
    assert parsed.thought == "I have the result."
    assert parsed.final_answer == "60"


def test_parses_action_and_json_arguments() -> None:
    parsed = parse_react_output(
        'Thought: compute it.\nAction: calculator\nAction Input: {"expression": "(17*3)+9"}'
    )
    assert parsed.kind == "action"
    assert parsed.action == "calculator"
    assert parsed.arguments == {"expression": "(17*3)+9"}
    assert parsed.error is None


def test_parses_fenced_output() -> None:
    parsed = parse_react_output(
        '```text\nThought: compute.\nAction: calculator\nAction Input: {"expression": "2+2"}\n```'
    )
    assert parsed.kind == "action"
    assert parsed.arguments == {"expression": "2+2"}


def test_tolerates_single_quotes_and_surrounding_prose() -> None:
    parsed = parse_react_output(
        "Thought: compute.\nAction: calculator\n"
        'Action Input: here is the input {"expression": "2+2"} thanks'
    )
    assert parsed.kind == "action"
    assert parsed.arguments == {"expression": "2+2"}

    parsed = parse_react_output(
        "Thought: compute.\nAction: calculator\nAction Input: {'expression': '2+2'}"
    )
    assert parsed.kind == "action"
    assert parsed.arguments == {"expression": "2+2"}


def test_single_string_argument_falls_back_to_raw_text() -> None:
    parsed = parse_react_output("Action: calculator\nAction Input: 2 + 2")
    assert parsed.kind == "action"
    assert parsed.arguments is None
    assert parsed.raw_input == "2 + 2"


def test_strips_observation_from_action_input() -> None:
    parsed = parse_react_output(
        'Action: calculator\nAction Input: {"expression": "2+2"}\nObservation: 4'
    )
    assert parsed.kind == "action"
    assert parsed.raw_input == '{"expression": "2+2"}'
    assert parsed.arguments == {"expression": "2+2"}


def test_final_answer_wins_over_later_action() -> None:
    parsed = parse_react_output("Action: calculator\nAction Input: {}\nFinal Answer: done")
    assert parsed.kind == "action"

    parsed = parse_react_output("Final Answer: done\nAction: calculator\nAction Input: {}")
    assert parsed.kind == "final"
    assert parsed.final_answer == "done"


def test_invalid_output_reports_error() -> None:
    parsed = parse_react_output("I am not sure what to do.")
    assert parsed.kind == "invalid"
    assert parsed.error
    assert parsed.raw_output == "I am not sure what to do."


def test_parse_action_input_empty_and_invalid() -> None:
    assert parse_action_input("") == ({}, None)
    value, error = parse_action_input("not json")
    assert value is None
    assert error
