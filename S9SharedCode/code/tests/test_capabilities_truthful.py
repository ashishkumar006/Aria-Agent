"""The capability document must not claim capabilities chat does not have.

`chat.tools` is a kill-switch that strips the tool payload from both chat
endpoints. `/api/capabilities` ignored it, so it kept advertising the full 21
tool list while the model was handed none. A client that trusts the capability
document then builds UI offering actions that can never fire - the same class
of lie as a receipt claiming an artifact that was never rendered.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent_server

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


def test_capabilities_advertise_tools_when_enabled(client, monkeypatch):
    import flags
    monkeypatch.setattr(flags, "is_enabled",
                        lambda key, default=True: True, raising=False)

    caps = client.get("/api/capabilities", headers=HDR).json()
    assert caps["tools"]["toolCalling"] is True
    assert len(caps["tools"]["tools"]) > 0
    assert caps["custom"]["chatToolsEnabled"] is True


def test_capabilities_withhold_tools_when_flag_off(client, monkeypatch):
    """The regression: with the kill-switch on, the capability document used
    to keep listing all 21 tools."""
    import flags
    monkeypatch.setattr(flags, "is_enabled",
                        lambda key, default=True: False, raising=False)

    caps = client.get("/api/capabilities", headers=HDR).json()
    assert caps["tools"]["tools"] == []
    assert caps["tools"]["toolCalling"] is False
    assert caps["custom"]["chatToolsEnabled"] is False


def test_capabilities_still_reports_agui_when_agui_flag_off(client, monkeypatch):
    """The AG-UI flag and the tools flag are independent. Turning tools off
    must not hide the fact that the endpoint speaks AG-UI at all."""
    import flags
    monkeypatch.setattr(
        flags, "is_enabled",
        lambda key, default=True: key != "chat.agui", raising=False)

    caps = client.get("/api/capabilities", headers=HDR).json()
    assert caps["custom"]["aguiEndpointEnabled"] is False
    assert caps["identity"]["agentName"] == "Aria"


def test_capabilities_never_advertise_a_tool_chat_lacks():
    """Whatever is advertised must be a subset of the real chat catalog."""
    from skills import tool_payload

    real = {t["name"] for t in tool_payload(agent_server._CHAT_TOOLS)}
    caps = TestClient(agent_server.app).get(
        "/api/capabilities", headers=HDR).json()
    advertised = set(caps["tools"]["tools"])
    assert advertised <= real, (
        f"capabilities advertises tools chat cannot call: {advertised - real}")
