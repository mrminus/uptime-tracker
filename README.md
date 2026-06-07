# Uptime Tracker

Pings a host every 5 seconds, records latency and packet loss, and serves a live web dashboard showing the last 24 hours of data. Keeps 7 days of history on disk.

---

## Requirements

- Ubuntu 20.04+ (or any systemd-based Linux)
- Python 3.8+
- `ping` binary (pre-installed on all Ubuntu systems)
- No pip packages required — pure Python stdlib

---

## File Layout

```
/home/<you>/uptime-tracker/          ← source / install files
    pinger.py                        ← ping daemon
    server.py                        ← web dashboard server
    mcp_server.py                    ← MCP server (query data from Claude)
    uptime-pinger.service            ← systemd unit (pinger)
    uptime-web.service               ← systemd unit (web)
    install.sh                       ← one-shot installer
    README.md                        ← this file

/opt/uptime-tracker/                 ← installed location (created by install.sh)
    pinger.py
    server.py
    data/
        uptime.db                    ← SQLite database (auto-created on first run)

/etc/systemd/system/
    uptime-pinger.service
    uptime-web.service
```

---

## Installation

Copy the source folder to the Ubuntu machine, then run the installer as root:

```bash
sudo bash install.sh
```

The installer will:

1. Create a locked-down system user `uptime-tracker` (no login shell, no home dir)
2. Copy `pinger.py` and `server.py` to `/opt/uptime-tracker/`
3. Install both `.service` files to `/etc/systemd/system/`
4. Run `systemctl enable --now` on both services (starts immediately + survives reboots)
5. Print the dashboard URL and a `systemctl status` summary

---

## Accessing the Dashboard

By default the dashboard binds to localhost for safety:

```
http://127.0.0.1:9090
```

To expose it on a trusted LAN, set `UPTIME_BIND=0.0.0.0` in `uptime-web.service`, then run `sudo systemctl daemon-reload && sudo systemctl restart uptime-web`. After that, open:

```
http://<server-ip>:9090
```

The page auto-refreshes every 5 seconds. No login is built in, so only expose it on networks you trust.

---

## Services

### `uptime-pinger` — the ping daemon

| Detail | Value |
|---|---|
| Script | `/opt/uptime-tracker/pinger.py` |
| Runs as | `uptime-tracker` (system user) |
| Interval | Every 5 seconds |
| Packets per sample | 3 (min/avg/max are computed from received reply RTTs) |
| What it stores | timestamp, min/avg/max latency (ms), packet loss %, packets sent/received |
| Cleanup | Deletes records older than 7 days, runs hourly |

#### How latency is measured

The pinger uses the system `ping` binary, but it no longer depends on the final `rtt min/avg/max/mdev` summary line for latency. Each sample runs:

```bash
ping -n -D -c <packets> -i 0.3 -W 2 -- <host>
```

The flags matter:

- `-n` keeps output numeric and avoids reverse-DNS formatting differences.
- `-D` includes kernel timestamps on each reply line.
- `-c` controls the packet count from `UPTIME_PACKETS`.
- `-i 0.3` spaces packets 300 ms apart inside one sample.
- `-W 2` waits up to 2 seconds for each reply.

`pinger.py` parses each received reply's `time=... ms` value and computes min/avg/max from those per-packet RTTs. Packet loss and sent/received counts still come from the ping summary when available; if a summary is missing but reply lines were captured, received count and loss are derived from the parsed replies. With no replies, latency fields are stored as `NULL` and packet loss is treated as 100%.

### `uptime-web` — the dashboard server

| Detail | Value |
|---|---|
| Script | `/opt/uptime-tracker/server.py` |
| Runs as | `uptime-tracker` (system user) |
| Bind address | `127.0.0.1` by default (`UPTIME_BIND`) |
| Port | 9090 |
| Endpoints | `GET /` — dashboard page, `GET /api/data` — JSON data |
| No external process deps | Reads directly from the SQLite database |

### Database — SQLite

| Detail | Value |
|---|---|
| Path | `/opt/uptime-tracker/data/uptime.db` |
| Format | SQLite 3 |
| Table | `pings` |
| Retention | 7 days (configurable) |
| Approx size | ~15 MB per week of data |

**Schema:**

```sql
CREATE TABLE pings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     REAL    NOT NULL,   -- Unix epoch (seconds)
    host          TEXT    NOT NULL,
    min_ms        REAL,               -- NULL when no response
    avg_ms        REAL,
    max_ms        REAL,
    packet_loss   REAL    NOT NULL,   -- 0.0 to 100.0
    packets_sent  INTEGER NOT NULL,
    packets_recv  INTEGER NOT NULL
);
```

---

## Configuration

All configuration is via environment variables set in the systemd unit files. Edit them with:

```bash
sudo systemctl edit uptime-pinger    # opens an override file
sudo systemctl edit uptime-web
sudo systemctl daemon-reload
sudo systemctl restart uptime-pinger uptime-web
```

### Pinger options (`uptime-pinger.service`)

| Variable | Default | Description |
|---|---|---|
| `UPTIME_HOST` | `8.8.8.8` | Host or IP to ping |
| `UPTIME_DB` | `/opt/uptime-tracker/data/uptime.db` | SQLite database path |
| `UPTIME_INTERVAL` | `5` | Seconds between samples |
| `UPTIME_PACKETS` | `3` | Packets sent per sample (min/avg/max are computed from received replies) |
| `UPTIME_RETENTION` | `7` | Days of data to keep |

### Web server options (`uptime-web.service`)

| Variable | Default | Description |
|---|---|---|
| `UPTIME_HOST` | `8.8.8.8` | Must match pinger (used to filter DB queries) |
| `UPTIME_DB` | `/opt/uptime-tracker/data/uptime.db` | SQLite database path |
| `UPTIME_PORT` | `9090` | Port the dashboard listens on |
| `UPTIME_LATENCY_WARN` | `100` | Latency (ms) that turns the indicator yellow |
| `UPTIME_LATENCY_CRIT` | `300` | Latency (ms) that turns the indicator red |

**Example override to change the monitored host:**

```ini
# /etc/systemd/system/uptime-pinger.service.d/override.conf
[Service]
Environment=UPTIME_HOST=192.168.1.1
```

---

## Operational Commands

### Start / Stop / Restart

```bash
sudo systemctl start   uptime-pinger uptime-web
sudo systemctl stop    uptime-pinger uptime-web
sudo systemctl restart uptime-pinger uptime-web
```

### Check status

```bash
sudo systemctl status uptime-pinger
sudo systemctl status uptime-web
```

### Follow live logs

```bash
sudo journalctl -u uptime-pinger -f    # pinger output (one line per measurement)
sudo journalctl -u uptime-web -f       # web server output
```

### View recent log history

```bash
sudo journalctl -u uptime-pinger --since "1 hour ago"
sudo journalctl -u uptime-pinger --since "2024-01-15 08:00"
```

### Query the database directly

```bash
# Install sqlite3 if needed: sudo apt install sqlite3

# Latest 10 measurements
sqlite3 /opt/uptime-tracker/data/uptime.db \
  "SELECT datetime(timestamp,'unixepoch','localtime'), avg_ms, packet_loss FROM pings ORDER BY id DESC LIMIT 10;"

# Count records per day
sqlite3 /opt/uptime-tracker/data/uptime.db \
  "SELECT date(timestamp,'unixepoch','localtime') as day, count(*) FROM pings GROUP BY day;"

# All outage periods (100% packet loss)
sqlite3 /opt/uptime-tracker/data/uptime.db \
  "SELECT datetime(timestamp,'unixepoch','localtime'), packet_loss FROM pings WHERE packet_loss=100 ORDER BY timestamp DESC LIMIT 50;"
```

---

## Monitoring Multiple Hosts

The current setup monitors one host. To monitor additional hosts:

1. Copy `pinger.py` to `/opt/uptime-tracker/pinger2.py`
2. Create a new service file `/etc/systemd/system/uptime-pinger2.service` based on the original, setting `UPTIME_HOST` to the second host
3. Run `sudo systemctl enable --now uptime-pinger2`

The web server currently displays data for the single `UPTIME_HOST` set in its config. All hosts share the same database (filtered by the `host` column).

---

## MCP Server (query data from Claude)

`mcp_server.py` is a [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Claude query your ping data directly. Once connected, you can ask questions like:

- *"Is 8.8.8.8 up right now?"*
- *"What was the average latency over the last 6 hours?"*
- *"Have there been any outages today?"*
- *"Show me the worst latency spikes this week"*

### Requirements

```bash
pip3 install mcp
```

### Tools exposed

| Tool | Description |
|---|---|
| `get_status` | Current ping status — latency, packet loss, up/degraded/down |
| `get_stats` | Summary stats over a time window: uptime %, avg/max latency, outage count |
| `get_events` | Outage and degraded-service events, most recent first |
| `get_history` | Full ping history with adaptive resolution (1-min averages beyond 1h, 5-sec raw for the last hour) |
| `list_hosts` | All monitored hosts and which is currently active |

### Connecting to Claude Desktop

Add the following to `~/.config/Claude/claude_desktop_config.json` (create it if it doesn't exist), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "uptime-tracker": {
      "command": "python3",
      "args": ["/home/<you>/uptime-tracker/mcp_server.py"],
      "env": {
        "UPTIME_DB": "/opt/uptime-tracker/data/uptime.db"
      }
    }
  }
}
```

Replace `/home/<you>/uptime-tracker/mcp_server.py` with the actual path on your machine. If running the tracker locally for development, set `UPTIME_DB` to match (e.g. `/tmp/test.db`).

### Running manually (test / debug)

The MCP server speaks JSON-RPC over stdio, so you can exercise it via the MCP inspector:

```bash
npx @modelcontextprotocol/inspector python3 mcp_server.py
```

Or run the pinger and server locally and point the MCP server at the same test database:

```bash
UPTIME_DB=/tmp/test.db python3 mcp_server.py
```

---

## Removal / Uninstall

```bash
# Stop and disable services
sudo systemctl stop    uptime-pinger uptime-web
sudo systemctl disable uptime-pinger uptime-web

# Remove service files
sudo rm /etc/systemd/system/uptime-pinger.service
sudo rm /etc/systemd/system/uptime-web.service
sudo systemctl daemon-reload
sudo systemctl reset-failed

# Remove installed files (including database)
sudo rm -rf /opt/uptime-tracker

# Remove system user
sudo userdel uptime-tracker
```

To keep the historical data before removing:

```bash
cp /opt/uptime-tracker/data/uptime.db ~/uptime-backup.db
```

---

## Troubleshooting

**Services won't start**
```bash
sudo journalctl -u uptime-pinger -n 50 --no-pager
```
Common cause: `/opt/uptime-tracker/data/` directory has wrong ownership. Fix:
```bash
sudo chown -R uptime-tracker:uptime-tracker /opt/uptime-tracker
```

**Dashboard shows "NO DATA"**
The web service started before the pinger wrote its first record. Wait 10 seconds and refresh, or check the pinger is running:
```bash
sudo systemctl status uptime-pinger
```

**"Permission denied" on ping**
The `ping` binary needs `cap_net_raw`. On Ubuntu it has this by default. Verify:
```bash
getcap /bin/ping
# Should show: /bin/ping cap_net_raw=ep
```

**Port 9090 already in use**
Change `UPTIME_PORT` in `uptime-web.service` to another port (e.g. 9091), then reload and restart.

**Database growing too large**
Reduce `UPTIME_RETENTION` (days). The cleanup runs hourly. To force an immediate cleanup:
```bash
sudo systemctl restart uptime-pinger
```
The cleanup runs at startup. Or run directly:
```bash
sqlite3 /opt/uptime-tracker/data/uptime.db \
  "DELETE FROM pings WHERE timestamp < strftime('%s','now','-7 days'); VACUUM;"
```
