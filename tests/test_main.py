"""Tests for the server's command-line entry point."""

import pytest
from mcp.server.mcpserver import MCPServer

from edgar_mcp.__main__ import main
from edgar_mcp.config import ENV_RATE, Toolset, bundled_snapshot


@pytest.fixture
def runs(monkeypatch):
    calls = []

    def fake_run(self, transport="stdio", **kwargs):
        calls.append((self, transport, kwargs))

    monkeypatch.setattr(MCPServer, "run", fake_run)
    return calls


def test_stdio_by_default(runs):
    assert main([]) == 0
    (_, transport, kwargs) = runs[0]
    assert (transport, kwargs) == ("stdio", {})


def test_http_transport_snapshot_and_toolset(runs, monkeypatch):
    built = []
    import edgar_mcp.__main__ as entry

    real_build = entry.build_server

    def spy(settings):
        built.append(settings)
        return real_build(settings)

    monkeypatch.setattr(entry, "build_server", spy)
    code = main(
        [
            "--transport",
            "streamable-http",
            "--port",
            "9100",
            "--snapshot",
            "bundled",
            "--toolset",
            "baseline",
        ]
    )
    assert code == 0
    (_, transport, kwargs) = runs[0]
    assert transport == "streamable-http"
    assert kwargs == {"host": "127.0.0.1", "port": 9100}
    assert built[0].snapshot_dir == bundled_snapshot()
    assert built[0].toolset is Toolset.BASELINE


def test_bad_environment_exits_with_usage_error(monkeypatch, capsys, runs):
    monkeypatch.setenv(ENV_RATE, "50")
    assert main([]) == 2
    assert "must be in" in capsys.readouterr().err
    assert runs == []
