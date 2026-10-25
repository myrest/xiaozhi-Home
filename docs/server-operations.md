# XiaoZhi Server Operations

This note records the device-facing endpoints and routine operations for this host. Replace `<host>` with the server address reachable from the device or browser; do not commit host-specific credentials.

## Endpoints

| Service | Endpoint | Purpose |
| --- | --- | --- |
| Manager and OTA | `http://<host>:16001` | Web console, device binding, OTA registration and address delivery |
| Voice WebSocket | `ws://<host>:16000/xiaozhi/v1/` | Device audio streaming; not an HTTP page |
| Vision analysis | `http://<host>:8003/mcp/vision/explain` | Vision-analysis endpoint |

The OTA URL is `http://<host>:16001/xiaozhi/ota/`; keep the trailing slash. When configuring a device to connect directly, use the WebSocket URL above.

## Device setup

- Audio: Opus, 24 kHz, mono, 60 ms frames.
- After Wi-Fi setup, the device reports its MAC address to OTA. Bind it in the manager under **Device Management** using the pairing code if prompted.
- When the host address changes, update `server.ota`, `server.websocket`, and `server.fronted_url` in the manager's parameter settings.
- `server.secret` must match `manager-api.secret` in the local `state/server/data/.config.yaml`. That file is runtime state and must not be committed or pasted into public logs.

## Routine commands

Run these from `~/xiaozhi`:

```bash
# Start or rebuild the unified stack
./scripts/deploy.sh

# Show status or follow server logs
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml logs -f xiaozhi-esp32-server

# Stop services without deleting persistent data
docker compose -f deploy/compose.yaml down

# Follow the application log
docker exec -it xiaozhi-esp32-server tail -f /opt/xiaozhi-esp32-server/tmp/server.log
```

Check OTA reachability with `curl -i http://<host>:16001/xiaozhi/ota/`. The browser should use the manager URL; the voice endpoint is WebSocket-only.

## Gemini tool-call failures

The server's Gemini provider uses `google-genai==2.24.0` and preserves each tool call's `thought_signature` when replaying the tool result. Keep the SDK pin in `services/server/requirements.txt` and `deploy/Dockerfile.server` aligned.

If a log shows an MCP tool succeeded immediately before `Function call is missing a thought_signature`, the MCP response already returned; inspect the Gemini provider history/replay and rebuild the server image with `./scripts/deploy.sh`. Rebuilding only the MCP image does not apply this fix. An MCP call that itself exceeds its endpoint timeout is a separate failure mode.
