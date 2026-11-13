"""Tests for the recursive config merge in config/config_loader.py."""
import asyncio

import pytest

import config.config_loader as config_loader
from config.config_loader import (
    _apply_local_mcp_endpoint,
    merge_configs,
    mcp_endpoint_call_url,
)


def test_local_mcp_endpoint_overrides_manager_endpoint(tmp_path, monkeypatch):
    endpoint_file = tmp_path / "xiaozhi_endpoint"
    endpoint_file.write_text(
        "ws://bedtime-mcp-endpoint:8004/mcp_endpoint/mcp/?token=secret\n"
    )
    monkeypatch.setenv("XIAOZHI_MCP_ENDPOINT_FILE", str(endpoint_file))
    config = {"mcp_endpoint": "wss://cloud.example/mcp"}

    _apply_local_mcp_endpoint(config)

    assert config["mcp_endpoint"] == (
        "ws://bedtime-mcp-endpoint:8004/mcp_endpoint/mcp/?token=secret"
    )


def test_local_endpoint_route_converts_to_call_and_preserves_query():
    endpoint = (
        "ws://bedtime-mcp-endpoint:8004/mcp_endpoint/mcp/"
        "?token=contains%2Fcall%2Bkey"
    )
    call_endpoint = mcp_endpoint_call_url(endpoint)

    assert call_endpoint == (
        "ws://bedtime-mcp-endpoint:8004/mcp_endpoint/call/"
        "?token=contains%2Fcall%2Bkey"
    )
    assert mcp_endpoint_call_url(call_endpoint) == call_endpoint


@pytest.mark.parametrize("contents", ["http://invalid/mcp", ""])
def test_invalid_local_endpoint_fails_closed(tmp_path, monkeypatch, contents):
    endpoint_file = tmp_path / "xiaozhi_endpoint"
    endpoint_file.write_text(contents)
    monkeypatch.setenv("XIAOZHI_MCP_ENDPOINT_FILE", str(endpoint_file))
    config = {"mcp_endpoint": "wss://cloud.example/mcp"}

    _apply_local_mcp_endpoint(config)

    assert config["mcp_endpoint"] == ""


def test_private_config_uses_call_route_after_local_secret_override(tmp_path, monkeypatch):
    endpoint_file = tmp_path / "xiaozhi_endpoint"
    endpoint_file.write_text(
        "ws://bedtime-mcp-endpoint:8004/mcp_endpoint/mcp/"
        "?token=abc%2Fdef%2Bghi\n"
    )
    monkeypatch.setenv("XIAOZHI_MCP_ENDPOINT_FILE", str(endpoint_file))

    async def get_agent_models(*args):
        return {"mcp_endpoint": "wss://manager.example/mcp/?token=ignored"}

    async def get_correct_words(*args):
        return None

    monkeypatch.setattr(config_loader, "get_agent_models", get_agent_models)
    monkeypatch.setattr(config_loader, "get_correct_words", get_correct_words)
    private_config = asyncio.run(
        config_loader.get_private_config_from_api(
            {"selected_module": {}}, "device-id", "client-id"
        )
    )

    assert private_config["mcp_endpoint"] == (
        "ws://bedtime-mcp-endpoint:8004/mcp_endpoint/call/"
        "?token=abc%2Fdef%2Bghi"
    )
def test_manager_mqtt_settings_survive_local_server_overrides(monkeypatch):
    async def get_server_config():
        return {
            "server": {
                "mqtt_gateway": "mqtt.example:1883",
                "mqtt_manager_api": "gateway:8007",
                "mqtt_signature_key": "manager-secret",
                "auth": {"enabled": False},
            }
        }

    monkeypatch.setattr(config_loader, "init_service", lambda _config: None)
    monkeypatch.setattr(config_loader, "get_server_config", get_server_config)
    result = asyncio.run(
        config_loader.get_config_from_api_async(
            {
                "manager-api": {"url": "http://manager", "secret": "manager-api-secret"},
                "server": {
                    "ip": "0.0.0.0",
                    "port": 8000,
                    "http_port": 8003,
                    "vision_explain": "http://server/vision",
                    "auth_key": "local-auth-key",
                    "mqtt_gateway": None,
                    "mqtt_signature_key": None,
                },
            }
        )
    )

    assert result["server"]["mqtt_gateway"] == "mqtt.example:1883"
    assert result["server"]["mqtt_manager_api"] == "gateway:8007"
    assert result["server"]["mqtt_signature_key"] == "manager-secret"

def test_explicit_local_mqtt_endpoint_overrides_manager(monkeypatch):
    async def get_server_config():
        return {"server": {"mqtt_gateway": "manager:1883", "auth": {"enabled": False}}}

    monkeypatch.setattr(config_loader, "init_service", lambda _config: None)
    monkeypatch.setattr(config_loader, "get_server_config", get_server_config)
    result = asyncio.run(
        config_loader.get_config_from_api_async(
            {
                "manager-api": {"url": "http://manager", "secret": "manager-api-secret"},
                "server": {
                    "ip": "0.0.0.0",
                    "port": 8000,
                    "http_port": 8003,
                    "mqtt_gateway": "local:1883",
                    "mqtt_signature_key": "local-key",
                },
            }
        )
    )

    assert result["server"]["mqtt_gateway"] == "local:1883"
    assert result["server"]["mqtt_signature_key"] == "local-key"



def test_merge_simple_override():
    default = {"a": 1, "b": 2}
    custom = {"b": 99}
    assert merge_configs(default, custom) == {"a": 1, "b": 99}


def test_merge_adds_new_key():
    default = {"a": 1}
    custom = {"b": 2}
    assert merge_configs(default, custom) == {"a": 1, "b": 2}


def test_merge_recursive_dict():
    default = {"x": {"a": 1, "b": 2}}
    custom = {"x": {"b": 99}}
    assert merge_configs(default, custom) == {"x": {"a": 1, "b": 99}}


def test_merge_deep_recursive():
    default = {"a": {"b": {"c": 1, "d": 2}}}
    custom = {"a": {"b": {"c": 99}}}
    assert merge_configs(default, custom) == {"a": {"b": {"c": 99, "d": 2}}}


def test_merge_replaces_non_dict_with_dict():
    default = {"a": 1}
    custom = {"a": {"nested": True}}
    assert merge_configs(default, custom) == {"a": {"nested": True}}


def test_merge_replaces_dict_with_non_dict():
    default = {"a": {"nested": True}}
    custom = {"a": 1}
    assert merge_configs(default, custom) == {"a": 1}


def test_merge_does_not_mutate_defaults():
    default = {"a": {"b": 1}}
    custom = {"a": {"c": 2}}
    merge_configs(default, custom)
    assert default == {"a": {"b": 1}}  # unchanged