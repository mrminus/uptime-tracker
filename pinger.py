#!/usr/bin/env python3
"""Uptime tracker pinger — pings a host every 5 seconds, stores latency metrics."""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import logging

DEFAULT_HOST  = os.environ.get('UPTIME_HOST', '8.8.8.8')
DB_PATH       = os.environ.get('UPTIME_DB',     '/opt/uptime-tracker/data/uptime.db')
CONFIG_PATH   = os.environ.get('UPTIME_CONFIG', '/opt/uptime-tracker/config.json')
INTERVAL      = int(os.environ.get('UPTIME_INTERVAL',  '5'))
PACKETS       = int(os.environ.get('UPTIME_PACKETS',   '3'))
RETENTION_DAYS = int(os.environ.get('UPTIME_RETENTION', '7'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def read_host() -> str:
    """Read the active host from config.json, falling back to the env var default."""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                return json.load(f).get('host', DEFAULT_HOST)
    except Exception:
        pass
    return DEFAULT_HOST


def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     REAL    NOT NULL,
            host          TEXT    NOT NULL,
            min_ms        REAL,
            avg_ms        REAL,
            max_ms        REAL,
            packet_loss   REAL    NOT NULL,
            packets_sent  INTEGER NOT NULL,
            packets_recv  INTEGER NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ts ON pings(timestamp)')
    conn.commit()
    conn.close()


def do_ping(host: str, count: int = 3):
    """Returns (min_ms, avg_ms, max_ms, packet_loss_pct, sent, recv)."""
    try:
        result = subprocess.run(
            ['ping', '-c', str(count), '-i', '0.3', '-W', '2', host],
            capture_output=True,
            text=True,
            timeout=count * 3 + 2,
        )
        out = result.stdout

        loss_m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
        packet_loss = float(loss_m.group(1)) if loss_m else 100.0

        seq_m = re.search(r'(\d+) packets transmitted, (\d+) (?:packets )?received', out)
        sent = int(seq_m.group(1)) if seq_m else count
        recv = int(seq_m.group(2)) if seq_m else 0

        rtt_m = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)', out)
        if rtt_m:
            return float(rtt_m.group(1)), float(rtt_m.group(2)), float(rtt_m.group(3)), packet_loss, sent, recv
        return None, None, None, packet_loss, sent, recv

    except subprocess.TimeoutExpired:
        return None, None, None, 100.0, count, 0
    except Exception as exc:
        log.error('Ping error: %s', exc)
        return None, None, None, 100.0, count, 0


def store(path: str, host: str, min_ms, avg_ms, max_ms, packet_loss, sent, recv) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        '''INSERT INTO pings
           (timestamp, host, min_ms, avg_ms, max_ms, packet_loss, packets_sent, packets_recv)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (time.time(), host, min_ms, avg_ms, max_ms, packet_loss, sent, recv),
    )
    conn.commit()
    conn.close()


def cleanup(path: str, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    conn = sqlite3.connect(path)
    deleted = conn.execute('DELETE FROM pings WHERE timestamp < ?', (cutoff,)).rowcount
    conn.commit()
    conn.close()
    if deleted:
        log.info('Cleaned up %d old records', deleted)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    init_db(DB_PATH)

    current_host = read_host()
    log.info('Pinging %s every %ds  db=%s', current_host, INTERVAL, DB_PATH)

    tick = 0
    while _running:
        t0 = time.time()

        # Pick up host changes written by the web UI
        new_host = read_host()
        if new_host != current_host:
            log.info('Host changed: %s → %s', current_host, new_host)
            current_host = new_host

        min_ms, avg_ms, max_ms, loss, sent, recv = do_ping(current_host, PACKETS)
        store(DB_PATH, current_host, min_ms, avg_ms, max_ms, loss, sent, recv)

        if avg_ms is not None:
            log.info('%s  avg=%.1fms min=%.1fms max=%.1fms loss=%.0f%%',
                     current_host, avg_ms, min_ms, max_ms, loss)
        else:
            log.warning('%s  NO RESPONSE  loss=%.0f%%', current_host, loss)

        tick += 1
        if tick % 720 == 0:   # ~every hour
            cleanup(DB_PATH, RETENTION_DAYS)

        elapsed = time.time() - t0
        time.sleep(max(0.1, INTERVAL - elapsed))


if __name__ == '__main__':
    main()
