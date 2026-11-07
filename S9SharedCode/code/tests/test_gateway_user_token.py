"""The agent must present its user token on every gateway call."""
import asyncio

import pytest

import agent_server
import gateway


def test_the_gateway_client_sends_the_user_token(tmp_path, monkeypatch):
    token_file = tmp_path / "gateway_user.token"
    token_file.write_text("aru_user_token_value", encoding="utf-8")
    monkeypatch.setenv("ARIA_GATEWAY_USER_TOKEN_FILE", str(token_file))
    client = gateway._RawLLM(base_url="http://127.0.0.1:1")
    headers = client.headers
    assert headers["X-Aria-User-Token"] == "aru_user_token_value"
    token_file.unlink()
    assert "X-Aria-User-Token" not in client.headers


def test_the_console_can_set_and_forget_the_user_token(tmp_path, monkeypatch):
    path = tmp_path / "gateway_user.token"
    monkeypatch.setattr(agent_server, "GATEWAY_USER_TOKEN_FILE", path)
    monkeypatch.setattr(agent_server, "_gateway_user_lookup",
                        lambda token: {"valid": True, "kind": "user",
                                       "user": {"id": "u-1", "name": "Ada",
                                                "keys": 2}})
    saved = asyncio.run(agent_server.set_gateway_user(
        {"token": "aru_console_token_value"}))
    assert saved["configured"] is True
    assert path.read_text(encoding="utf-8") == "aru_console_token_value"
    status = asyncio.run(agent_server.gateway_user_status())
    assert status["valid"] is True
    assert status["user"]["keys"] == 2
    assert asyncio.run(agent_server.clear_gateway_user()) == {"configured": False}
    assert not path.exists()


def test_a_rejected_token_is_not_saved(tmp_path, monkeypatch):
    path = tmp_path / "gateway_user.token"
    monkeypatch.setattr(agent_server, "GATEWAY_USER_TOKEN_FILE", path)
    monkeypatch.setattr(agent_server, "_gateway_user_lookup",
                        lambda token: {"valid": False, "error": "rejected"})
    from fastapi.responses import JSONResponse
    r = asyncio.run(agent_server.set_gateway_user(
        {"token": "aru_console_token_value"}))
    assert isinstance(r, JSONResponse) and r.status_code == 400
    assert not path.exists()
