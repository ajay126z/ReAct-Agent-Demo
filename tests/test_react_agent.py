from __future__ import annotations

from typing import Any

from react_mcp.agent.llm import LLMResponse
from react_mcp.agent.memory import ConversationTurn
from react_mcp.agent.react import ReActAgent
from react_mcp.mcp_client.base import ToolResult, ToolSpec


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, *, stop=None, temperature=None) -> LLMResponse:
        self.calls.append([dict(message) for message in messages])
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        return LLMResponse(
            content=self.replies.pop(0),
            thinking=None,
            prompt_tokens=5,
            completion_tokens=3,
            duration_ms=1.0,
        )


class FakeClient:
    def __init__(self, tools: list[ToolSpec], results: dict[str, ToolResult] | None = None) -> None:
        self.tools = tools
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> list[ToolSpec]:
        return self.tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        progress_callback: Any = None,
    ) -> ToolResult:
        self.calls.append((name, arguments))
        if name in self.results:
            return self.results[name]
        return ToolResult(content="ok")


def calculator_tool() -> ToolSpec:
    return ToolSpec(
        name="calculator",
        title="Calculator",
        description="Evaluate arithmetic.",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    )


def make_agent(settings, llm: FakeLLM, client: FakeClient) -> ReActAgent:
    return ReActAgent(
        llm=llm,
        client=client,
        tools=[calculator_tool()],
        settings=settings,
        show_steps=False,
    )


async def test_agent_injects_prior_qa_history(settings) -> None:
    llm = FakeLLM(["Final Answer: 6"])
    client = FakeClient([calculator_tool()])
    agent = make_agent(settings, llm, client)
    history = [ConversationTurn.create("What is 2+2?", "4", model="test-model")]

    result = await agent.run("And 3+3?", history=history)

    assert result.answer == "6"
    messages = llm.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "What is 2+2?"}
    assert messages[2] == {"role": "assistant", "content": "Final Answer: 4"}
    assert messages[3] == {"role": "user", "content": "And 3+3?"}


async def test_agent_runs_action_then_final(settings) -> None:
    llm = FakeLLM(
        [
            'Thought: compute it.\nAction: calculator\nAction Input: {"expression": "17*3+9"}',
            "Thought: done.\nFinal Answer: 60",
        ]
    )
    client = FakeClient([calculator_tool()], {"calculator": ToolResult(content="60")})
    agent = make_agent(settings, llm, client)

    result = await agent.run("What is (17*3)+9?")

    assert result.answer == "60"
    assert result.steps == 2
    assert result.stopped_reason == "final_answer"
    assert result.metrics.tool_calls == 1
    assert result.metrics.llm_calls == 2
    assert client.calls == [("calculator", {"expression": "17*3+9"})]
    assert result.transcript[0]["observation"] == "60"
    assert "Observation: 60" in llm.calls[1][-1]["content"]


async def test_unknown_tool_is_fed_back_and_agent_recovers(settings) -> None:
    llm = FakeLLM(
        [
            "Thought: try a tool.\nAction: missing\nAction Input: {}",
            "Thought: recovered.\nFinal Answer: I only have calculator.",
        ]
    )
    client = FakeClient([calculator_tool()])
    agent = make_agent(settings, llm, client)

    result = await agent.run("Use a tool")

    assert result.answer == "I only have calculator."
    assert result.metrics.tool_errors == 1
    assert result.metrics.tool_calls == 0
    assert "unknown tool 'missing'" in result.transcript[0]["observation"]
    assert "calculator" in result.transcript[0]["observation"]


async def test_bad_arguments_are_fed_back(settings) -> None:
    pair = ToolSpec(
        name="pair",
        title=None,
        description="Takes two values.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a", "b"],
        },
    )
    llm = FakeLLM(
        [
            "Action: pair\nAction Input: single-value",
            "Final Answer: could not do it",
        ]
    )
    client = FakeClient([pair])
    agent = ReActAgent(
        llm=llm,
        client=client,
        tools=[pair],
        settings=settings,
        show_steps=False,
    )

    result = await agent.run("Use pair")

    assert result.answer == "could not do it"
    assert result.metrics.tool_errors == 1
    assert "must be a JSON object" in result.transcript[0]["observation"]
    assert client.calls == []


async def test_tool_error_becomes_observation(settings) -> None:
    llm = FakeLLM(
        [
            'Action: calculator\nAction Input: {"expression": "1/0"}',
            "Final Answer: division failed",
        ]
    )
    client = FakeClient(
        [calculator_tool()],
        {"calculator": ToolResult(content="Division by zero.", is_error=True)},
    )
    agent = make_agent(settings, llm, client)

    result = await agent.run("Divide by zero")

    assert result.metrics.tool_errors == 1
    assert result.transcript[0]["is_error"] is True
    assert "Error from tool 'calculator'" in result.transcript[0]["observation"]


async def test_parse_retry_then_success(settings) -> None:
    llm = FakeLLM(["This is not the right format.", "Thought: ok.\nFinal Answer: 42"])
    client = FakeClient([calculator_tool()])
    agent = make_agent(settings, llm, client)

    result = await agent.run("Answer 42")

    assert result.answer == "42"
    assert result.metrics.parse_retries == 1
    assert result.steps == 2


async def test_parse_error_triggers_forced_final(settings) -> None:
    settings = settings.model_copy(update={"max_parse_retries": 0})
    llm = FakeLLM(["nonsense", "Final Answer: best effort"])
    client = FakeClient([calculator_tool()])
    agent = make_agent(settings, llm, client)

    result = await agent.run("Do something")

    assert result.answer == "best effort"
    assert result.stopped_reason == "forced_final"
    assert result.metrics.parse_retries == 1


async def test_step_limit_forces_final(settings) -> None:
    settings = settings.model_copy(update={"max_steps": 1})
    llm = FakeLLM(
        [
            'Action: calculator\nAction Input: {"expression": "2+2"}',
            "Final Answer: 4",
        ]
    )
    client = FakeClient([calculator_tool()], {"calculator": ToolResult(content="4")})
    agent = make_agent(settings, llm, client)

    result = await agent.run("2+2")

    assert result.answer == "4"
    assert result.stopped_reason == "forced_final"
    assert result.metrics.tool_calls == 1
    assert result.steps == 1
