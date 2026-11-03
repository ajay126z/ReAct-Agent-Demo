from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from react_mcp.tools import Toolbox


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("(17 * 3) + 9", "60"),
        ("2 ** 10", "1024"),
        ("1 / 4", "0.25"),
        ("-3 + 10", "7"),
        ("7 // 2", "3"),
        ("7 % 2", "1"),
        ("  (2 + 3) * 4  ", "20"),
    ],
)
def test_calculator_valid(settings, expression: str, expected: str) -> None:
    assert Toolbox(settings).calculator(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "__import__('os').system('echo hi')",
        "open('/etc/passwd')",
        "1 +",
        "hello",
        "2 ** 101",
        "1 / 0",
        "True + 1",
        "[1, 2, 3]",
    ],
)
def test_calculator_rejects_unsafe_or_invalid(settings, expression: str) -> None:
    with pytest.raises(ToolError):
        Toolbox(settings).calculator(expression)


def test_calculator_rejects_long_expression(settings) -> None:
    with pytest.raises(ToolError, match="too long"):
        Toolbox(settings).calculator("1 + " * 100)
