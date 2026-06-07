#!/usr/bin/env python3
"""MCP server for uptime-tracker — exposes ping/latency data as queryable tools."""

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from ipaddress import ip_address

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

DB_PATH     = os.environ.get('UPTIME_DB',     '/opt/uptime-tracker/data/uptime.db')
CONFIG_PATH = os.environ.get('UPTIME_CONFIG', '/opt/uptime-tracker/config.json')
DEFAULT_HOST = os.environ.get('UPTIME_HOST',  '8.8.8.8')
INTERVAL     = int(os.environ.get('UPTIME_INTERVAL', '5'))

LATENCY_WARN_MS = float(os.environ.get('UPTIME_LATENCY_WARN', '100'))
LATENCY_CRIT_MS = float(os.environ.get('UPTIME_LATENCY_CRIT', '300'))

HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}\.?$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)'
    r'(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$'
)

# ---------------------------------------------------------------------------
# Data layer (mirrors server.py)
# ---------------------------------------------------------------------------

def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _active_host() -> str:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
                host = cfg.get('host', DEFAULT_HOST)
                if _is_valid_host(host):
                    return host.strip()
    except Exception:
        pass
    return DEFAULT_HOST


def _is_valid_host(host) -> bool:
    if not isinstance(host, str):
        return False
    host = host.strip()
    if not host or len(host) > 253 or host.startswith('-') or any(c.isspace() for c in host):
        return False
    try:
        ip_address(host)
        return True
    except ValueError:
        return bool(HOSTNAME_RE.fullmatch(host))


def _known_hosts() -> list[str]:
    if not os.path.exists(DB_PATH):
        return []
    conn = _conn()
    rows = conn.execute('SELECT DISTINCT host FROM pings ORDER BY host').fetchall()
    conn.close()
    return [r['host'] for r in rows]


def _current(host: str) -> dict | None:
    if not os.path.exists(DB_PATH):
        return None
    conn = _conn()
    row = conn.execute(
        'SELECT * FROM pings WHERE host = ? ORDER BY timestamp DESC LIMIT 1',
        (host,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    age  = time.time() - row['timestamp']
    loss = row['packet_loss']
    avg  = row['avg_ms']
    if loss >= 100:
        status = 'down'
    elif loss > 0 or (avg and avg >= LATENCY_CRIT_MS):
        status = 'degraded'
    elif avg and avg >= LATENCY_WARN_MS:
        status = 'degraded'
    else:
        status = 'up'
    return {
        'host':        host,
        'timestamp':   row['timestamp'],
        'time_utc':    datetime.utcfromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S UTC'),
        'status':      status,
        'stale':       age > 30,
        'age_seconds': round(age),
        'min_ms':      row['min_ms'],
        'avg_ms':      row['avg_ms'],
        'max_ms':      row['max_ms'],
        'packet_loss': row['packet_loss'],
    }


def _history(host: str, hours: int = 24) -> list[dict]:
    """Adaptive resolution: 1-min buckets for >1h ago, raw 5s for the last hour."""
    if not os.path.exists(DB_PATH):
        return []
    now          = time.time()
    since        = now - hours * 3600
    one_hour_ago = now - 3600
    conn = _conn()
    older = conn.execute(
        '''SELECT
               CAST(timestamp / 60 AS INTEGER) * 60 AS bucket,
               MIN(CASE WHEN min_ms IS NOT NULL THEN min_ms END) AS min_ms,
               AVG(CASE WHEN avg_ms IS NOT NULL THEN avg_ms END) AS avg_ms,
               MAX(CASE WHEN max_ms IS NOT NULL THEN max_ms END) AS max_ms,
               CASE WHEN SUM(packets_sent) > 0
                    THEN CAST(SUM(packets_sent - packets_recv) * 100.0
                              / SUM(packets_sent) AS REAL)
                    ELSE 100.0
               END AS packet_loss,
               COUNT(*) AS samples,
               SUM(packets_sent) AS packets_sent,
               SUM(packets_recv) AS packets_recv,
               60 AS interval_s
           FROM pings
           WHERE timestamp > ? AND timestamp <= ? AND host = ?
           GROUP BY bucket ORDER BY bucket''',
        (since, one_hour_ago, host),
    ).fetchall()
    recent = conn.execute(
        '''SELECT timestamp AS bucket, min_ms, avg_ms, max_ms, packet_loss,
                  1 AS samples, packets_sent, packets_recv, ? AS interval_s
           FROM pings WHERE timestamp > ? AND host = ? ORDER BY timestamp''',
        (INTERVAL, one_hour_ago, host),
    ).fetchall()
    conn.close()
    return [dict(r) for r in older] + [dict(r) for r in recent]


def _stats(history: list[dict]) -> dict:
    if not history:
        return {'uptime_pct': None, 'avg_ms': None, 'max_ms': None,
                'outage_count': 0, 'total_samples': 0}
    total   = sum(int(r.get('samples') or 1) for r in history)
    sent    = sum(int(r.get('packets_sent') or 0) for r in history)
    recv    = sum(int(r.get('packets_recv') or 0) for r in history)
    avg_rows = [(r['avg_ms'], int(r.get('samples') or 1))
                for r in history if r['avg_ms'] is not None]
    all_max = [r['max_ms'] for r in history if r['max_ms'] is not None]
    outages, in_outage = 0, False
    for r in history:
        if r['packet_loss'] >= 100:
            if not in_outage:
                outages += 1
                in_outage = True
        else:
            in_outage = False
    return {
        'uptime_pct':    round(recv / sent * 100, 2) if sent else None,
        'avg_ms':        round(sum(avg * samples for avg, samples in avg_rows)
                               / sum(samples for _, samples in avg_rows), 1) if avg_rows else None,
        'max_ms':        round(max(all_max), 1) if all_max else None,
        'outage_count':  outages,
        'total_samples': total,
    }


def _events(history: list[dict]) -> list[dict]:
    events, current = [], None
    for bucket in history:
        loss = bucket['packet_loss']
        ts   = bucket['bucket']
        interval_s = int(bucket.get('interval_s') or 60)
        if loss > 0:
            if current is None:
                current = {
                    'start':    ts,
                    'start_utc': datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S UTC'),
                    'end':      ts + interval_s,
                    'max_loss': loss,
                    'kind':     'outage' if loss >= 100 else 'degraded',
                    'samples':  1,
                }
            else:
                current['end']      = ts + interval_s
                current['max_loss'] = max(current['max_loss'], loss)
                current['samples'] += 1
                if loss >= 100:
                    current['kind'] = 'outage'
        else:
            if current is not None:
                current['duration_s'] = current['end'] - current['start']
                current['end_utc']    = datetime.utcfromtimestamp(current['end']).strftime('%Y-%m-%d %H:%M:%S UTC')
                if current['max_loss'] >= 100 or current['samples'] >= 3:
                    events.append(current)
                current = None
    if current is not None:
        current['end']        = None
        current['end_utc']    = 'ongoing'
        current['duration_s'] = None
        if current['max_loss'] >= 100 or current['samples'] >= 3:
            events.append(current)
    events.reverse()
    return events[:50]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("uptime-tracker")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_status",
            description=(
                "Get the current ping status for a host. Returns latency (min/avg/max ms), "
                "packet loss %, and whether the host is 'up', 'degraded', or 'down'. "
                "Omit host to query the currently active/monitored host."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Hostname or IP to query. Omit to use the active host.",
                    }
                },
            },
        ),
        types.Tool(
            name="get_stats",
            description=(
                "Get summary statistics for a host over a time window: uptime percentage, "
                "average and max latency, and number of outages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host to query. Omit to use the active host.",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How many hours back to analyse (default 24, max 168).",
                        "default": 24,
                    },
                },
            },
        ),
        types.Tool(
            name="get_events",
            description=(
                "Get outage and degraded-service events for a host, most recent first. "
                "Each event shows its type (outage / degraded), start/end time, duration, "
                "and max packet loss."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host to query. Omit to use the active host.",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How many hours back to look (default 24, max 168).",
                        "default": 24,
                    },
                },
            },
        ),
        types.Tool(
            name="get_history",
            description=(
                "Get the full ping history for a host with adaptive resolution: "
                "1-minute averages for data older than 1 hour, raw 5-second samples for "
                "the most recent hour. Useful for spotting latency trends or spikes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Host to query. Omit to use the active host.",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How many hours back to return (default 24, max 168).",
                        "default": 24,
                    },
                },
            },
        ),
        types.Tool(
            name="list_hosts",
            description=(
                "List every host that has ever been monitored and which one is currently active."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:
    def text(obj) -> list[types.TextContent]:
        return [types.TextContent(type="text", text=json.dumps(obj, indent=2))]

    if name == "list_hosts":
        active = _active_host()
        hosts  = _known_hosts()
        return text({"active_host": active, "known_hosts": hosts})

    if name == "get_status":
        host   = arguments.get("host") or _active_host()
        if not _is_valid_host(host):
            return text({"error": "valid hostname or IP address required"})
        result = _current(host)
        if result is None:
            return text({"error": f"No data found for host '{host}'. Is the pinger running?"})
        return text(result)

    if name in ("get_stats", "get_events", "get_history"):
        host  = arguments.get("host") or _active_host()
        if not _is_valid_host(host):
            return text({"error": "valid hostname or IP address required"})
        hours = min(int(arguments.get("hours", 24)), 168)
        hist  = _history(host, hours)
        if not hist:
            return text({"error": f"No history found for host '{host}' in the last {hours}h."})

        if name == "get_stats":
            stats = _stats(hist)
            stats["host"]  = host
            stats["hours"] = hours
            return text(stats)

        if name == "get_events":
            events = _events(hist)
            return text({"host": host, "hours": hours, "events": events})

        if name == "get_history":
            # Summarise row count to avoid overwhelming Claude's context
            return text({
                "host":    host,
                "hours":   hours,
                "samples": len(hist),
                "history": hist,
            })

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
