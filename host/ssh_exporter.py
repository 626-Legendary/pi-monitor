#!/usr/bin/env python3
"""Export host SSH sessions and successful logins to an atomic /run snapshot.

Uses only the Python standard library. Run on the systemd host, not in Docker.
No listener, command execution endpoint, or power-control capability is exposed.
"""
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

LOGGER = logging.getLogger("pi_monitor.ssh")
OUTPUT = Path("/run/pi-monitor-ssh/ssh.json")
INTERVAL = 10
HISTORY_INTERVAL = 60
HISTORY_LIMIT = 20
JOURNAL_LIMIT = 2000
ACCEPTED = re.compile(r"^Accepted (\S+) for (\S+) from (\S+) port (\d+)\b")


def run_command(arguments):
    result = subprocess.run(
        arguments, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=8,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC", "SYSTEMD_COLORS": "0"},
    )
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} failed (exit {result.returncode})")
    if "not seeing messages" in result.stderr or "insufficient permissions" in result.stderr:
        raise PermissionError("Journal access is incomplete")
    return result.stdout


def parse_session_time(value):
    for pattern in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S.%f %Z"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def parse_sessions(output):
    sessions = []
    for block in re.split(r"\n\s*\n", output.strip()):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Remote") != "yes" or fields.get("Service") not in {"sshd", "sshd-session"}:
            continue
        if fields.get("State") == "closing":
            continue
        sessions.append({
            "user": fields.get("Name", ""),
            "source_ip": fields.get("RemoteHost", ""),
            "terminal": fields.get("TTY", ""),
            "session_id": fields.get("Id", ""),
            "login_at": parse_session_time(fields.get("Timestamp", "")),
        })
    return sorted(sessions, key=lambda item: (item["login_at"] or 0), reverse=True)


def collect_sessions():
    listing = run_command(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
    session_ids = [line.split()[0] for line in listing.splitlines() if line.split()]
    if not session_ids:
        return []
    if len(session_ids) > 256 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", item) for item in session_ids):
        raise RuntimeError("Unexpected session list")
    properties = ["Id", "Name", "Remote", "RemoteHost", "Service", "Timestamp", "TTY", "State"]
    output = run_command([
        "loginctl", "show-session", "--no-pager",
        *[f"--property={prop}" for prop in properties], "--", *session_ids,
    ])
    return parse_sessions(output)


def parse_history(output):
    items = []
    seen = set()
    for line in output.splitlines():
        try:
            entry = json.loads(line)
            message = entry.get("MESSAGE")
            if not isinstance(message, str):
                continue
            match = ACCEPTED.match(message)
            if match is None:
                continue
            method, user, source_ip, port = match.groups()
            login_at = int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
            if not 0 <= login_at <= 253402300799:
                continue
            key = (login_at, user, source_ip, port, method)
            if key in seen:
                continue
            seen.add(key)
            items.append({"user": user, "source_ip": source_ip,
                          "source_port": int(port), "method": method, "login_at": login_at})
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    return sorted(items, key=lambda item: item["login_at"], reverse=True)[:HISTORY_LIMIT]


def collect_history():
    output = run_command([
        "journalctl", "--no-pager", "--output=json", "--since=-30days",
        f"--lines={JOURNAL_LIMIT}", "--identifier=sshd", "--identifier=sshd-session",
    ])
    return parse_history(output)


def refresh_section(previous, collector, error_message):
    try:
        return {"items": collector(), "updated_at": time.time(), "error": None}
    except (OSError, RuntimeError, subprocess.SubprocessError):
        LOGGER.exception(error_message)
        return {**previous, "error": error_message}


def write_snapshot(output, snapshot):
    # Directory must exist; systemd RuntimeDirectory owns its lifecycle.
    temporary = output.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False, allow_nan=False)
    temporary.chmod(0o644)
    temporary.replace(output)


def serve(stop, output=OUTPUT):
    sessions = {"items": [], "updated_at": None, "error": None}
    history = dict(sessions)
    next_history = 0
    while not stop.is_set():
        started = time.monotonic()
        sessions = refresh_section(sessions, collect_sessions, "无法读取 SSH 会话，请检查 systemd-logind 服务。")
        if started >= next_history:
            history = refresh_section(history, collect_history, "无法读取 SSH 登录记录，请检查日志读取权限。")
            next_history = time.monotonic() + (INTERVAL if history["error"] else HISTORY_INTERVAL)
        try:
            write_snapshot(output, {"version": 1, "collected_at": time.time(),
                                    "sessions": sessions, "history": history})
        except OSError:
            LOGGER.exception("Unable to write SSH snapshot")
        stop.wait(max(1, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stopping.set())
    serve(stopping)
