from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from react_mcp.tools import Toolbox, _assert_public_url, _html_to_markdown


def test_assert_public_url_rejects_bad_scheme_and_creds() -> None:
    with pytest.raises(ToolError, match="Only http"):
        _assert_public_url("file:///etc/passwd")
    with pytest.raises(ToolError, match="embedded credentials"):
        _assert_public_url("https://user:pass@example.com/")
    with pytest.raises(ToolError, match="empty"):
        _assert_public_url("")


def test_assert_public_url_blocks_private_literal_ips() -> None:
    for url in (
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
    ):
        with pytest.raises(ToolError, match="not a public IP"):
            _assert_public_url(url)


def test_assert_public_url_allows_public_ip_literal() -> None:
    assert _assert_public_url("https://93.184.216.34/") == "https://93.184.216.34/"


def test_assert_public_url_blocks_private_dns(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(2, 1, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ToolError, match="non-public IP"):
        _assert_public_url("https://internal.example/")


def test_html_to_markdown_converter() -> None:
    html_text = (
        "<html><head><title>T</title><script>evil()</script></head>"
        "<body><h1>Hello</h1><p>World <a href='https://example.com'>link</a>.</p>"
        "<ul><li>One</li><li>Two</li></ul><p><strong>bold</strong></p></body></html>"
    )
    md = _html_to_markdown(html_text)
    assert "# Hello" in md
    assert "[link](https://example.com)" in md
    assert "- One" in md
    assert "**bold**" in md
    assert "evil" not in md


def test_web_fetch_markdown(settings, monkeypatch) -> None:
    box = Toolbox(settings)
    import react_mcp.tools as tools_mod

    body = b"<html><body><h1>Hi</h1><p>Text</p></body></html>"
    monkeypatch.setattr(tools_mod, "_assert_public_url", lambda u: u)
    monkeypatch.setattr(
        tools_mod,
        "_fetch_bytes",
        lambda url, **kwargs: (200, "text/html", body, {"content-type": "text/html"}),
    )
    result = box.web_fetch("https://example.com", "auto")
    assert result["status"] == 200
    assert result["format"] == "markdown"
    assert "# Hi" in result["content"]
    assert result["truncated"] is False
    assert "untrusted" in result["note"]


def test_web_fetch_text_from_plain(settings, monkeypatch) -> None:
    box = Toolbox(settings)
    import react_mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_assert_public_url", lambda u: u)
    monkeypatch.setattr(
        tools_mod,
        "_fetch_bytes",
        lambda url, **kwargs: (200, "text/plain", b"hello world", {"content-type": "text/plain"}),
    )
    result = box.web_fetch("https://example.com/page.txt", "text")
    assert result["format"] == "text"
    assert result["content"] == "hello world"


def test_web_fetch_follows_redirect_and_rechecks(settings, monkeypatch) -> None:
    box = Toolbox(settings)
    import react_mcp.tools as tools_mod

    calls = {"n": 0}

    def fake_fetch(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (301, "", b"", {"location": "https://example.com/final"})
        return (200, "text/plain", b"final", {"content-type": "text/plain"})

    monkeypatch.setattr(tools_mod, "_assert_public_url", lambda u: u)
    monkeypatch.setattr(tools_mod, "_fetch_bytes", fake_fetch)
    result = box.web_fetch("https://example.com/start", "auto")
    assert result["url"] == "https://example.com/final"
    assert result["content"] == "final"
    assert calls["n"] == 2


def test_web_fetch_http_error_and_bad_format(settings, monkeypatch) -> None:
    box = Toolbox(settings)
    import react_mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_assert_public_url", lambda u: u)
    monkeypatch.setattr(
        tools_mod,
        "_fetch_bytes",
        lambda url, **kwargs: (404, "text/html", b"not found", {"content-type": "text/html"}),
    )
    with pytest.raises(ToolError, match="HTTP 404"):
        box.web_fetch("https://example.com/missing", "auto")
    with pytest.raises(ToolError, match="format must be"):
        box.web_fetch("https://example.com/", "pdf")


def test_web_fetch_truncates_large_body(settings, monkeypatch) -> None:
    small_settings = settings.model_copy(
        update={"web_fetch_max_bytes": 1000, "web_fetch_max_chars": 20}
    )
    box = Toolbox(small_settings)
    import react_mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_assert_public_url", lambda u: u)
    monkeypatch.setattr(
        tools_mod,
        "_fetch_bytes",
        lambda url, **kwargs: (200, "text/plain", b"a" * 5000, {"content-type": "text/plain"}),
    )
    result = box.web_fetch("https://example.com/big", "text")
    assert result["truncated"] is True
    assert len(result["content"]) == 20


def test_web_fetch_disabled(settings) -> None:
    disabled = settings.model_copy(update={"web_fetch_enabled": False})
    box = Toolbox(disabled)
    with pytest.raises(ToolError, match="disabled"):
        box.web_fetch("https://example.com", "auto")
    with pytest.raises(ToolError, match="disabled"):
        box.web_search("hello", 3)


BING_FIXTURE = (
    "<html><body><ol id='b_results'>"
    "<li class='b_algo'><h2><a href='https://www.bing.com/ck/a?!&amp;&amp;"
    "p=xxx&amp;u=a1aHR0cHM6Ly9leGFtcGxlLmNvbS8='>Example Domain</a></h2>"
    "<p>Example domain snippet text.</p></li>"
    "<li class='b_algo'><h2><a href='https://python.org/about/'>Python</a></h2>"
    "<p>Python snippet.</p></li>"
    "</ol></body></html>"
)


def test_web_search_parses_fixture(settings, monkeypatch) -> None:
    box = Toolbox(settings)
    import react_mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_assert_public_url", lambda u: u)
    monkeypatch.setattr(
        tools_mod,
        "_fetch_bytes",
        lambda url, **kwargs: (
            200,
            "text/html",
            BING_FIXTURE.encode(),
            {"content-type": "text/html"},
        ),
    )
    result = box.web_search("example", 5)
    assert result["query"] == "example"
    assert len(result["results"]) == 2
    assert result["results"][0]["url"] == "https://example.com/"
    assert result["results"][0]["snippet"] == "Example domain snippet text."
    assert result["results"][1]["title"] == "Python"


def test_web_search_rejects_empty_query(settings) -> None:
    box = Toolbox(settings)
    with pytest.raises(ToolError, match="empty"):
        box.web_search("   ", 3)
