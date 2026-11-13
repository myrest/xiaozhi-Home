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
- `server.websocket` is sent to the device as a complete URL; set exactly one `/xiaozhi/v1/` path (for example, `wss://<host>/xiaozhi/v1/` behind public TLS, or `ws://<LAN-host>:16000/xiaozhi/v1/` for direct LAN access). Do not store a host URL with a trailing slash and append the path separately.
- With a TLS reverse proxy, route `/xiaozhi/v1/` to host port `16000` and preserve WebSocket `Upgrade`/`Connection` headers; route `/xiaozhi/ota/` to host port `16001`. A Cloudflare `502` means the public request is not reaching a healthy origin.
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

## One-time reminders

Reminders run in the separate `reminder-mcp` service; they do not execute IoT commands. The MCP tools available to the assistant are:

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `reminder_create` | Required `time`; optional `message` and `timezone` | Create a one-time reminder for the currently connected device. Use a positive seconds string for relative time (for example, `"180"` for three minutes), or an ISO 8601 timestamp with an explicit UTC offset for calendar time. |
| `reminder_list` | None | List reminders belonging to the current device. |
| `reminder_cancel` | `reminder_id` | Cancel that device's reminder, including an active reminder. |

`timezone` defaults to `Asia/Taipei`. A missing or empty message produces `現在時間：HH:MM` for the reminder's due time; an explicit message uses that fixed text. The backend synthesizes speech with the device's configured TTS profile, converts it to mono 24 kHz Ogg Opus, then publishes a signed HTTPS audio URL and subtitle to the MQTT Gateway. The ESP32 owns audio playback and the ringing loop; the server does not play reminders over its audio WebSocket.

Every reminder has a fixed window from `due_at` until `expires_at = due_at + 180` seconds; late delivery uses only the remaining time. The backend retries or republishes the MQTT event until a terminal device receipt or expiry; the Gateway does not queue offline messages, so the device must reconnect before expiry. While busy, the device can receive the event only if its MQTT connection remains established. MQTT publish is QoS 0 and does not prove delivery. The firmware must deduplicate by `reminder_id` and `revision`, fetch the URL, stop ringing by `expires_at`, and acknowledge progress/terminal state. `completed`, `stopped`, or `expired` receipts update the durable reminder record. A `reminder_cancel` sends `notify_cancel` until the device acknowledges it as stopped.

### MQTT notification delivery

The optional Compose profile `reminder-mqtt` adds the patched MQTT Gateway. Export `MQTT_SIGNATURE_KEY` and `REMINDER_AUDIO_PUBLIC_URL` in the deployment environment; set `MQTT_SIGNATURE_KEY` to the same value as Manager's `server.mqtt_signature_key`. Set `server.mqtt_manager_api` to the internal API origin `mqtt-gateway:8007`. If WebSocket authentication is enabled, `MQTT_SERVER_SECRET` must match the server's configured auth key. The Manager parameter rows may exist while their values are empty; the service is not operational until these values and the Gateway are configured.

For the Tunnel, route the public `/mqtt` WebSocket upgrade to `http://127.0.0.1:8007`, and route `/reminders/audio/*` to the server's HTTP origin on port `8003`. Keep the existing `/xiaozhi/v1/` voice route pointed at the server. The Compose profile binds Gateway port `8007` to loopback and does not publish raw MQTT TCP or UDP ports; do not expose `/api/notifications` publicly.

Start the Gateway after its key and Tunnel routes are configured:

```bash
docker compose -f deploy/compose.yaml --profile reminder-mqtt up -d --build mqtt-gateway
```

The device event is `{"type":"notify","reminder_id":"<UUID>","revision":0,"audio_url":"https://<host>/reminders/audio/<UUID>/<revision>.ogg?...","subtitle":"...","expires_at":0}`. The firmware must keep its MQTT control connection alive while busy, deduplicate by `(reminder_id, revision)`, fetch the signed URL before expiry, play mono Ogg Opus, ring only until `expires_at`, and publish `{"type":"notify_ack","reminder_id":"<UUID>","revision":0,"status":"received"}` to `device-server`. Progress statuses are `received` and `playing`; terminal notify statuses are `completed`, `stopped`, and `expired`. For `notify_cancel`, publish `status:"stopped"`; the Gateway reports that receipt as `cancelled`.

The upstream Gateway also starts its voice WebSocket bridge and UDP audio path when a device sends its normal MQTT `hello`. Cloudflare Tunnel routes WSS/HTTPS here, not generic UDP. Do not change the OTA `server.mqtt_gateway` endpoint to `wss://<host>/mqtt` until the deployed firmware's WSS MQTT support and notification-only behavior are confirmed; otherwise the device may switch its voice transport to the Gateway's UDP path.

### Private scheduling interface

The reminder scheduler submits an authoritative snapshot every second with a five-second HTTP timeout:

```text
POST http://xiaozhi-esp32-server:8006/internal/reminders/sync
Authorization: Bearer <shared token>
```

The body contains `{"reminders":[...],"cancellations":[...]}`. Each pending reminder has `id`, `device_id`, `due_at`, `expires_at`, `timezone`, and `message`; each due cancellation has `id`, `device_id`, `due_at`, `expires_at`, and `revision`. The response's `outcomes` list carries device terminal ACKs (`completed`, `stopped`, `expired`) or a confirmed cancellation (`cancelled`). Only outcomes for records in that exact snapshot are applied. Pending rows remain available through a 300-second acknowledgement grace after expiry; cancellation rows are retried until device ACK or their expiry window ends. Omitting a record cancels its active delivery.

Port 8006 is not published on the host. `reminder-mcp` and the server share the internal `reminder-notifications` network; MCP transport uses the existing internal `mcp-backend` network. The server reads `XIAOZHI_REMINDER_TOKEN_FILE=/run/secrets/reminder_token`. The reminder service reads `REMINDER_TOKEN_FILE` at the same path, `REMINDER_DATABASE=/data/reminders.sqlite3`, `REMINDER_ENDPOINT_FILE=/run/secrets/mcp_endpoint`, and `REMINDER_NOTIFICATION_URL` at the URL above. Both use `TZ=Asia/Taipei`. The server injects `_device_id` and `_token` into reminder calls and removes those fields from assistant-visible schemas; the assistant must never select a device identity or receive the shared token.

### Deployment and persistence

Run `./scripts/deploy.sh` as the existing UID 1000 deployment user, or as root. The script prepares private UID/GID `1000:1000` MCP state directories (mode `0700`) and uses the endpoint bootstrap to create `state/mcp/reminder_token` from cryptographically secure randomness only if it does not already exist. The token is mode `0600`, mounted read-only into both services, and is never printed. Existing endpoint credentials and the reminder token are preserved across deployments; bootstrap refuses to replace an invalid existing reminder token. Treat the token and endpoint URL files as secrets: do not paste them into logs, commands, or issue reports.

The schedule lives in the bind-mounted `state/mcp/reminders/` directory and survives container replacement. Back up that directory consistently with SQLite (or with `reminder-mcp` stopped) and protect backups just like other private runtime state. Keep the shared token when restoring the services together. `docker compose down` does not delete these bind mounts.

```bash
# Provision private state, rebuild images, and start both MCP services
./scripts/deploy.sh

# Follow the reminder scheduler without printing credentials
docker compose -f deploy/compose.yaml logs -f reminder-mcp

# Restart the reminder process without deleting its schedule
docker compose -f deploy/compose.yaml restart reminder-mcp
```

The reminder image reuses the digest-pinned Python 3.13/Alpine 3.22 base and `services/mcp/requirements.lock.txt` (`mcp==1.30.0`, `httpx==0.28.1`, `websockets==15.0.1`); Alpine `tzdata` supplies `ZoneInfo("Asia/Taipei")`. It includes the existing `bedtime_mcp.bridge` transport alongside `reminder_mcp`.

Both MCP processes intentionally use the same private `tool_endpoint` file. The endpoint is pinned at [`0f9302f327100fb976c7bdea6e4ebbb8ebdb5787`](https://github.com/xinnan-tech/mcp-endpoint-server/tree/0f9302f327100fb976c7bdea6e4ebbb8ebdb5787): its [`ConnectionManager`](https://github.com/xinnan-tech/mcp-endpoint-server/blob/0f9302f327100fb976c7bdea6e4ebbb8ebdb5787/src/core/connection_manager.py) stores multiple MCP connections per agent, aggregates `tools/list` results, and finds the owning connection by tool name; its [`WebSocketHandler`](https://github.com/xinnan-tech/mcp-endpoint-server/blob/0f9302f327100fb976c7bdea6e4ebbb8ebdb5787/src/handlers/websocket_handler.py) routes `tools/call` to that connection. The existing `bedtime_*` and new `reminder_*` names do not collide. After both services connect, a fresh device session discovers both sets of tools.
