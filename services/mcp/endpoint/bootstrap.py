"""Create persistent endpoint credentials and URLs without logging secrets."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, "/app/upstream")

CONFIG_FILE = Path("/app/upstream/data/.mcp-endpoint-server.cfg")
URL_DIR = Path("/run/endpoint-urls")
MCP_ENDPOINT_PATH = "/mcp_endpoint/mcp/"


def _endpoint_urls(external_base: str, token: str) -> dict[str, str]:
    """Use the server config route; XiaoZhi rewrites its /mcp/ path to /call/."""
    return {
        "tool_endpoint": f"ws://endpoint:8004{MCP_ENDPOINT_PATH}?token={token}",
        "xiaozhi_endpoint": f"ws://{external_base}{MCP_ENDPOINT_PATH}?token={token}",
    }


def _atomic_private_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _config(key: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["server"] = {
        "host": "0.0.0.0",
        "port": "8004",
        "debug": "false",
        "log_level": "WARNING",
        "key": key,
    }
    cfg["websocket"] = {
        "max_connections": "1000",
        "ping_interval": "30",
        "ping_timeout": "10",
        "close_timeout": "10",
    }
    cfg["security"] = {"allowed_origins": "", "enable_cors": "false"}
    cfg["logging"] = {"log_file": "", "max_file_size": "10MB", "backup_count": "5"}
    return cfg


def _existing_key() -> str | None:
    if not CONFIG_FILE.exists():
        return None
    cfg = configparser.ConfigParser()
    with CONFIG_FILE.open(encoding="utf-8") as source:
        cfg.read_file(source)
    key = cfg.get("server", "key", fallback="")
    if len(key.encode("utf-8")) not in (16, 24, 32):
        raise RuntimeError("Existing endpoint key is invalid; refusing to replace it")
    return key


def bootstrap() -> None:
    from src.utils.aes_utils import encrypt

    agent_id = os.environ.get("XIAOZHI_AGENT_ID", "").strip()
    if not agent_id:
        raise RuntimeError("XIAOZHI_AGENT_ID is required")
    external_host = os.environ.get("XIAOZHI_ENDPOINT_HOST", "bedtime-mcp-endpoint").strip()
    if not external_host or "/" in external_host or ":" in external_host:
        raise RuntimeError("XIAOZHI_ENDPOINT_HOST must be a DNS name or IPv4/IPv6 host")

    key = _existing_key() or secrets.token_hex(16)
    cfg = _config(key)
    from io import StringIO

    serialized = StringIO()
    cfg.write(serialized)
    _atomic_private_write(CONFIG_FILE, serialized.getvalue())

    # Manager encryptToken hashes the raw agent ID once before AES encryption.
    identity = hashlib.md5(agent_id.encode("utf-8")).hexdigest()
    token = quote(
        encrypt(key, json.dumps({"agentId": identity}, separators=(",", ":"))), safe=""
    )
    external_base = f"{external_host}:8004"
    endpoints = _endpoint_urls(external_base, token)
    _atomic_private_write(URL_DIR / "tool_endpoint", endpoints["tool_endpoint"] + "\n")
    _atomic_private_write(URL_DIR / "xiaozhi_endpoint", endpoints["xiaozhi_endpoint"] + "\n")
    _atomic_private_write(
        URL_DIR / "health_url",
        f"http://{external_base}/mcp_endpoint/health?key={key}\n",
    )
    print("Endpoint credentials initialized; URLs are stored in the private state directory.")


if __name__ == "__main__":
    bootstrap()
