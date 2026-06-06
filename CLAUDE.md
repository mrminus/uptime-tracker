# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two-script Python tool (stdlib only, no pip dependencies) that pings a host every 5 seconds, stores results in SQLite, and serves a live web dashboard on port 9090. Runs as a pair of systemd services on Ubuntu 20.04+.

## Running locally (development)

Run either script directly against a local database:

```bash
UPTIME_DB=/tmp/test.db python3 pinger.py
UPTIME_DB=/tmp/test.db python3 server.py
```

The database and its parent directory are auto-created on first run.

## Deployed service management

After `sudo bash install.sh`, the services run as the `uptime-tracker` system user from `/opt/uptime-tracker/`.

```bash
# Logs
sudo journalctl -u uptime-pinger -f
sudo journalctl -u uptime-web -f

# Restart after editing installed scripts
sudo systemctl restart uptime-pinger uptime-web

# Config changes (env vars in service files)
sudo systemctl edit uptime-pinger    # opens an override file
sudo systemctl daemon-reload && sudo systemctl restart uptime-pinger uptime-web
```

## Architecture

### Data flow
`pinger.py` → SQLite `pings` table → `server.py` reads directly (no IPC)

### Host switching at runtime
Both scripts read the active host from `config.json` (default `/opt/uptime-tracker/config.json`) on every iteration, not just at startup. The web UI's `POST /api/config` writes this file; the pinger picks up the change within one polling interval (~5s) without restarting.

### `pinger.py`
- Calls the system `ping` binary via `subprocess` and parses its stdout with regex
- Opens and closes a fresh SQLite connection per write (keeps the file unlocked between samples)
- Runs hourly cleanup (every 720 ticks at 5s intervals) to delete records older than `UPTIME_RETENTION` days

### `server.py`
- Single-file: stdlib `http.server.BaseHTTPRequestHandler` + the full dashboard HTML/JS embedded as a string constant (`DASHBOARD_HTML`)
- Template substitution: `__WARN_MS__`, `__CRIT_MS__`, and `__PRESETS__` in the HTML string are replaced at request time with server-side values
- **Adaptive resolution in `get_history()`**: data older than 1 hour is bucketed into 1-minute averages (SQL `GROUP BY`); the last hour is returned as raw 5-second rows. Both ranges are fetched in a single connection and concatenated.
- Status thresholds (`LATENCY_WARN_MS`, `LATENCY_CRIT_MS`) are read from env vars and injected into the page at render time — the JS `WARN_MS`/`CRIT_MS` constants on the client mirror these.

### API endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard HTML |
| GET | `/api/data` | JSON: current status, 24h stats, history, events |
| GET | `/api/config` | JSON: active host, known hosts, presets |
| POST | `/api/config` | Change monitored host (writes `config.json`) |
| GET | `/api/health` | `{"ok": true}` |

### Environment variables
All configuration is via env vars set in the `.service` files. `UPTIME_HOST` in the service file is only the startup default; the runtime-active host comes from `config.json` after the first write via the UI.

## SQLite schema

```sql
CREATE TABLE pings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    REAL    NOT NULL,   -- Unix epoch seconds
    host         TEXT    NOT NULL,
    min_ms       REAL,               -- NULL on no response
    avg_ms       REAL,
    max_ms       REAL,
    packet_loss  REAL    NOT NULL,   -- 0.0–100.0
    packets_sent INTEGER NOT NULL,
    packets_recv INTEGER NOT NULL
);
CREATE INDEX idx_ts ON pings(timestamp);
```

Multiple hosts share the same table, filtered by the `host` column.
