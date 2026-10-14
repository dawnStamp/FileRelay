
import pytest

from apps.base.models import KeyValue


@pytest.mark.asyncio
async def test_mcp_initialize_and_list_tools(initialized_client):
    init_response = await initialized_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert init_response.status_code == 200
    assert init_response.json()["result"]["capabilities"]["tools"]

    tools_response = await initialized_client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert tools_response.status_code == 200
    assert {tool["name"] for tool in tools_response.json()["result"]["tools"]} == {
        "share_text",
        "share_file",
    }


@pytest.mark.asyncio
async def test_mcp_rejects_cross_origin_requests(initialized_client):
    response = await initialized_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        headers={"origin": "https://attacker.invalid"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_mcp_token_can_be_required(initialized_client):
    from core.settings import settings

    record = await KeyValue.filter(key="settings").first()
    config = dict(record.value)
    config["mcp_require_token"] = 1
    record.value = config
    await record.save()
    settings.user_config = config

    response = await initialized_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "share_text", "arguments": {"text": "private"}},
        },
    )
    assert response.status_code == 401
    from apps.admin.dependencies import create_token

    token = create_token({"is_admin": True})
    authorized = await initialized_client.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "share_text", "arguments": {"text": "private"}},
        },
    )
    assert authorized.status_code == 200
    assert "/#/?code=" in authorized.json()["result"]["structuredContent"]["url"]
