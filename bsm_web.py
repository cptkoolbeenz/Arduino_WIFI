#!/usr/bin/env python3
"""Minimal web UI to start/stop normal BSM operations."""

from __future__ import annotations

import html
import csv
import socket
import subprocess
import sys
import threading
import datetime as dt
import time
from urllib.parse import parse_qs
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DISCOVER_CONTROL_PORT = 8888
WEB_SET_TIME_OFFSET_HOURS = -4.0
TZ_PRESET_OFFSETS: dict[str, float] = {
    "ast": -4.0,
    "adt": -3.0,
    "est": -5.0,
    "edt": -4.0,
}
UI_STATE_LOCK = threading.Lock()
LAST_SET_TIME_OFFSET_HOURS = WEB_SET_TIME_OFFSET_HOURS
LAST_SET_TIME_PRESET = "edt"


def get_last_set_time_state() -> tuple[float, str]:
    with UI_STATE_LOCK:
        return LAST_SET_TIME_OFFSET_HOURS, LAST_SET_TIME_PRESET


def set_last_set_time_state(offset_hours: float, preset: str) -> None:
    global LAST_SET_TIME_OFFSET_HOURS, LAST_SET_TIME_PRESET
    with UI_STATE_LOCK:
        LAST_SET_TIME_OFFSET_HOURS = offset_hours
        LAST_SET_TIME_PRESET = preset


NORMAL_OPS_CMD = [
    sys.executable,
    "-u",
    "bsm_network.py",
    "--scheduled",
    "--discover",
    "--discover-csv",
    "data/discovered_devices.csv",
    "--transfer-latest-file",
    "--prefer-file-prefix",
    "TR",
    "--file-day",
    "yesterday",
    "--no-sync-time",
    "--transfer-tolerant",
    "--cloud-enabled",
]


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._log_path = Path("data/web_normal_ops.log")
        self._force_proc: subprocess.Popen[str] | None = None
        self._force_log_path = Path("data/web_force_upload.log")

    def status(self) -> tuple[bool, int | None]:
        with self._lock:
            if self._proc is None:
                return False, None
            if self._proc.poll() is not None:
                self._proc = None
                return False, None
            return True, self._proc.pid

    def force_status(self) -> tuple[bool, int | None]:
        with self._lock:
            if self._force_proc is None:
                return False, None
            if self._force_proc.poll() is not None:
                self._force_proc = None
                return False, None
            return True, self._force_proc.pid

    def start(self) -> str:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return f"Normal Ops already running (PID {self._proc.pid})."

            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = self._log_path.open("a", encoding="utf-8")
            self._proc = subprocess.Popen(
                NORMAL_OPS_CMD,
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return f"Normal Ops started (PID {self._proc.pid})."

    def stop(self) -> str:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                return "Normal Ops is not running."

            proc = self._proc
            proc.terminate()

        try:
            proc.wait(timeout=5)
            msg = f"Normal Ops stopped (PID {proc.pid})."
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
            msg = f"Normal Ops force-stopped (PID {proc.pid})."

        with self._lock:
            self._proc = None
        return msg

    def start_force_upload(self, uid: str, device_ip: str) -> str:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return "Stop Normal Ops before force upload (port/bind conflict)."
            if self._force_proc is not None and self._force_proc.poll() is None:
                return f"Force upload already running (PID {self._force_proc.pid})."

            self._force_log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = self._force_log_path.open("a", encoding="utf-8")
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            logf.write(f"\n=== Force upload start {stamp} uid={uid} ip={device_ip} ===\n")
            logf.flush()

            cmd = [
                sys.executable,
                "-u",
                "bsm_network.py",
                "--discover",
                "--discover-ip",
                device_ip,
                "--discover-attempts",
                "3",
                "--discover-timeout",
                "8",
                "--discover-interval",
                "0.3",
                "--post-poll-wait",
                "0",
                "--discover-csv",
                "data/discovered_devices.csv",
                "--transfer-latest-file",
                "--prefer-file-prefix",
                "TR",
                "--file-day",
                "latest",
                "--transfer-latest-even-if-seen",
                "--no-sync-time",
                "--transfer-tolerant",
                "--mark-partial-received",
                "--no-cloud-enabled",
            ]
            self._force_proc = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return f"Force upload started for {uid} ({device_ip}) (PID {self._force_proc.pid})."

    def shutdown(self) -> None:
        self.stop()
        with self._lock:
            if self._force_proc is not None and self._force_proc.poll() is None:
                self._force_proc.terminate()


MANAGER = ProcessManager()


def read_log_tail(path: Path, max_bytes: int = 120_000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def _iso_to_dt(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def read_devices_status(path: Path, online_seconds: int = 600) -> str:
    rows = read_devices_rows(path, online_seconds=online_seconds)
    if not rows:
        return "(No devices discovered yet)"

    lines = []
    lines.append("status   unique_id                              network_uid       device_ip      recv_ip        last_seen")
    lines.append("------   ------------------------------------   ---------------   -----------   -----------    -------------------")
    for row in rows:
        status = row.get("status", "UNKNOWN")
        uid = row.get("unique_id", "")
        net_uid = row.get("network_uid", "")
        dev_ip = row.get("device_ip", "")
        recv_ip = row.get("recv_ip", "")
        last_seen_raw = row.get("last_seen", "")
        lines.append(f"{status:<6}   {uid:<36}   {net_uid:<15}   {dev_ip:<11}   {recv_ip:<11}    {last_seen_raw}")
    return "\n".join(lines)


def read_devices_rows(path: Path, online_seconds: int = 600) -> list[dict[str, str]]:
    if not path.exists():
        return []

    rows: list[dict[str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_clean = {k: (v or "").strip() for k, v in row.items()}
            rows.append(row_clean)

    now = dt.datetime.now()
    for row in rows:
        status = "UNKNOWN"
        last_seen = _iso_to_dt(row.get("last_seen", ""))
        if last_seen is not None:
            age_s = (now - last_seen).total_seconds()
            status = "ONLINE" if age_s <= online_seconds else "STALE"
        row["status"] = status
    return rows


def query_device_time(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(b"GET_TIME", (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            line = data.decode("utf-8", errors="replace").strip()
            if src_ip != device_ip:
                return f"RTC query got reply from unexpected source: {src_ip} ({line})"
            if line.startswith("TIME,"):
                parts = line.split(",", 2)
                epoch = parts[1] if len(parts) > 1 else "?"
                ts = parts[2] if len(parts) > 2 else "?"
                if attempt == 1 and first_line.startswith("ERR_TIME"):
                    return (
                        f"RTC query recovered for {device_ip}: initial error ({first_line}), "
                        f"then OK after reset/retry: {ts} (epoch={epoch})"
                    )
                return f"RTC query OK from {device_ip}: {ts} (epoch={epoch})"
            if attempt == 0:
                first_line = line
                continue
            return f"RTC query error from {device_ip}: {line} (after initial: {first_line})"
        return f"RTC query error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"RTC query timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"RTC query failed for {device_ip}: {exc}"
    finally:
        sock.close()


def set_device_time(device_ip: str, offset_hours: float, timeout_s: float = 2.0) -> str:
    epoch = int(time.time() + (offset_hours * 3600.0))
    msg = f"SET_TIME,{epoch}".encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(msg, (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            line = data.decode("utf-8", errors="replace").strip()
            if src_ip != device_ip:
                return f"SET_TIME got reply from unexpected source: {src_ip} ({line})"
            if line.startswith("ACK_TIME,"):
                verify_msg = query_device_time(device_ip=device_ip, timeout_s=timeout_s)
                if attempt == 1 and first_line:
                    return (
                        f"SET_TIME recovered for {device_ip}: initial error ({first_line}), then ACK ({line}). "
                        f"Verify: {verify_msg}"
                    )
                return (
                    f"SET_TIME OK for {device_ip}: {line} "
                    f"(offset={offset_hours:+g}h). Verify: {verify_msg}"
                )
            if attempt == 0:
                first_line = line
                continue
            return f"SET_TIME error from {device_ip}: {line} (after initial: {first_line})"
        return f"SET_TIME error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"SET_TIME timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"SET_TIME failed for {device_ip}: {exc}"
    finally:
        sock.close()


def ping_device(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"PING", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        if src_ip != device_ip:
            return f"PING got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
        line = data.decode("utf-8", errors="replace").strip()
        return f"PING OK from {device_ip}: {line}"
    except socket.timeout:
        return f"PING timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"PING failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_status(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_STATUS", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        if src_ip != device_ip:
            return f"GET_STATUS got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
        line = data.decode("utf-8", errors="replace").strip()
        return f"GET_STATUS OK from {device_ip}: {line}"
    except socket.timeout:
        return f"GET_STATUS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_STATUS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_config(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_CONFIG", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        if src_ip != device_ip:
            return f"GET_CONFIG got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
        line = data.decode("utf-8", errors="replace").strip()
        return f"GET_CONFIG OK from {device_ip}: {line}"
    except socket.timeout:
        return f"GET_CONFIG timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_CONFIG failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_diagnostics(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_DIAGNOSTICS", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        if src_ip != device_ip:
            return f"GET_DIAGNOSTICS got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
        line = data.decode("utf-8", errors="replace").strip()
        return f"GET_DIAGNOSTICS OK from {device_ip}: {line}"
    except socket.timeout:
        return f"GET_DIAGNOSTICS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_DIAGNOSTICS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_last_data(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_LAST_DATA", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        if src_ip != device_ip:
            return f"GET_LAST_DATA got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
        line = data.decode("utf-8", errors="replace").strip()
        return f"GET_LAST_DATA OK from {device_ip}: {line}"
    except socket.timeout:
        return f"GET_LAST_DATA timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_LAST_DATA failed for {device_ip}: {exc}"
    finally:
        sock.close()


def set_device_config(device_ip: str, config_updates: dict[str, str], timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        params = ",".join(f"{k}={v}" for k, v in config_updates.items())
        msg = f"SET_CONFIG,{params}".encode("utf-8")
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(msg, (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            if src_ip != device_ip:
                return f"SET_CONFIG got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
            line = data.decode("utf-8", errors="replace").strip()
            if attempt == 0:
                first_line = line
                continue
            if line.startswith("ACK_CONFIG"):
                return f"SET_CONFIG OK for {device_ip}: {line}"
            return f"SET_CONFIG error from {device_ip}: {line} (after initial: {first_line})"
        return f"SET_CONFIG error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"SET_CONFIG timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"SET_CONFIG failed for {device_ip}: {exc}"
    finally:
        sock.close()


def reboot_device(device_ip: str, timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(b"REBOOT", (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            if src_ip != device_ip:
                return f"REBOOT got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
            line = data.decode("utf-8", errors="replace").strip()
            if attempt == 0:
                first_line = line
                continue
            if line.startswith("ACK_REBOOT"):
                return f"REBOOT OK for {device_ip}: {line}"
            return f"REBOOT error from {device_ip}: {line} (after initial: {first_line})"
        return f"REBOOT error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"REBOOT timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"REBOOT failed for {device_ip}: {exc}"
    finally:
        sock.close()


def enter_data_mode(device_ip: str, timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(b"ENTER_DATA_MODE", (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            if src_ip != device_ip:
                return f"ENTER_DATA_MODE got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
            line = data.decode("utf-8", errors="replace").strip()
            if attempt == 0:
                first_line = line
                continue
            if line.startswith("ACK_ENTER_DATA_MODE"):
                return f"ENTER_DATA_MODE OK for {device_ip}: {line}"
            return f"ENTER_DATA_MODE error from {device_ip}: {line} (after initial: {first_line})"
        return f"ENTER_DATA_MODE error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"ENTER_DATA_MODE timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"ENTER_DATA_MODE failed for {device_ip}: {exc}"
    finally:
        sock.close()


def clear_device_errors(device_ip: str, timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        first_line = ""
        for attempt in range(2):
            sock.sendto(b"CLEAR_ERRORS", (device_ip, DISCOVER_CONTROL_PORT))
            data, (src_ip, _src_port) = sock.recvfrom(2048)
            if src_ip != device_ip:
                return f"CLEAR_ERRORS got reply from unexpected source: {src_ip} ({data.decode('utf-8', errors='replace').strip()})"
            line = data.decode("utf-8", errors="replace").strip()
            if attempt == 0:
                first_line = line
                continue
            if line.startswith("ACK_CLEAR_ERRORS"):
                return f"CLEAR_ERRORS OK for {device_ip}: {line}"
            return f"CLEAR_ERRORS error from {device_ip}: {line} (after initial: {first_line})"
        return f"CLEAR_ERRORS error from {device_ip}: {first_line}"
    except socket.timeout:
        return f"CLEAR_ERRORS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"CLEAR_ERRORS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def render_page(message: str = "") -> bytes:
    running, pid = MANAGER.status()
    forcing, force_pid = MANAGER.force_status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    force_state = f"RUNNING (PID {force_pid})" if forcing else "IDLE"
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    online = [d for d in devices if d.get("status") == "ONLINE"]
    last_offset, last_preset = get_last_set_time_state()
    msg_html = f"<p><strong>{html.escape(message)}</strong></p>" if message else ""
    if online:
        opts = []
        for d in online:
            uid = d.get("unique_id", "")
            ip = d.get("device_ip", "") or d.get("recv_ip", "")
            label = f"{uid[-6:]} @ {ip} ({uid})"
            value = f"{uid}|{ip}"
            opts.append(f'<option value="{html.escape(value)}">{html.escape(label)}</option>')
        force_select_html = "\n".join(opts)
        force_form_html = f"""
    <form method="post" action="/force-upload" style="display:block; margin-top:0.75rem;">
      <label for="device">Force latest TR from:</label>
      <select id="device" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Force Upload Latest TR</button>
    </form>
    <form method="post" action="/query-time" style="display:block; margin-top:0.5rem;">
      <label for="device_time">Query RTC time from:</label>
      <select id="device_time" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Query RTC Time</button>
    </form>
    <form method="post" action="/set-time" style="display:block; margin-top:0.5rem;">
      <label for="device_set_time">Set RTC time for:</label>
      <select id="device_set_time" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <label for="tz_preset">Zone:</label>
      <select id="tz_preset" name="tz_preset" style="margin:0 0.5rem;">
        <option value="ast" data-offset="{TZ_PRESET_OFFSETS["ast"]}" {"selected" if last_preset == "ast" else ""}>AST</option>
        <option value="adt" data-offset="{TZ_PRESET_OFFSETS["adt"]}" {"selected" if last_preset == "adt" else ""}>ADT</option>
        <option value="est" data-offset="{TZ_PRESET_OFFSETS["est"]}" {"selected" if last_preset == "est" else ""}>EST</option>
        <option value="edt" data-offset="{TZ_PRESET_OFFSETS["edt"]}" {"selected" if last_preset == "edt" else ""}>EDT</option>
        <option value="manual" data-offset="{last_offset}" {"selected" if last_preset == "manual" else ""}>Manual</option>
      </select>
      <label for="offset_hours">Offset (hrs):</label>
      <input id="offset_hours" name="offset_hours" type="number" step="0.5" value="{last_offset}" style="width:5rem; margin:0 0.5rem;" />
      <button type="submit">Set Arduino Time</button>
    </form>
    <form method="post" action="/ping" style="display:block; margin-top:0.5rem;">
      <label for="device_ping">Ping:</label>
      <select id="device_ping" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Ping Device</button>
    </form>
    <form method="post" action="/get-status" style="display:block; margin-top:0.5rem;">
      <label for="device_status">Get status from:</label>
      <select id="device_status" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Get Status</button>
    </form>
    <form method="post" action="/get-config" style="display:block; margin-top:0.5rem;">
      <label for="device_config">Get config from:</label>
      <select id="device_config" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Get Config</button>
    </form>
    <form method="post" action="/get-diag" style="display:block; margin-top:0.5rem;">
      <label for="device_diag">Get diagnostics from:</label>
      <select id="device_diag" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Get Diagnostics</button>
    </form>
    <form method="post" action="/get-last-data" style="display:block; margin-top:0.5rem;">
      <label for="device_data">Get last data from:</label>
      <select id="device_data" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Get Last Data</button>
    </form>
    <form method="post" action="/set-config" style="display:block; margin-top:0.5rem;">
      <label for="device_set_config">Set config for:</label>
      <select id="device_set_config" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <label for="start_hour">START_HOUR:</label>
      <input id="start_hour" name="start_hour" type="number" min="0" max="23" style="width:4rem; margin:0 0.5rem;" />
      <label for="end_hour">END_HOUR:</label>
      <input id="end_hour" name="end_hour" type="number" min="0" max="23" style="width:4rem; margin:0 0.5rem;" />
      <button type="submit">Set Config</button>
    </form>
    <form method="post" action="/reboot" style="display:block; margin-top:0.5rem;">
      <label for="device_reboot">Reboot:</label>
      <select id="device_reboot" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Reboot Device</button>
    </form>
    <form method="post" action="/enter-data-mode" style="display:block; margin-top:0.5rem;">
      <label for="device_enter">Enter data mode:</label>
      <select id="device_enter" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Enter Data Mode</button>
    </form>
    <form method="post" action="/clear-errors" style="display:block; margin-top:0.5rem;">
      <label for="device_clear">Clear errors:</label>
      <select id="device_clear" name="device" style="margin:0 0.5rem;">
        {force_select_html}
      </select>
      <button type="submit">Clear Errors</button>
    </form>
"""
    else:
        force_form_html = '<p style="margin-top:0.75rem;"><em>No ONLINE Arduinos currently listed.</em></p>'
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BSM Controller</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; }}
    .panel {{ max-width: 980px; border: 1px solid #ccc; padding: 1rem 1.25rem; border-radius: 8px; }}
    .status {{ margin: 0.75rem 0 1rem 0; }}
    form {{ display: inline-block; margin-right: 0.75rem; }}
    button {{ font-size: 1rem; padding: 0.6rem 1rem; cursor: pointer; }}
    #logbox {{
      margin-top: 1rem;
      border: 1px solid #bbb;
      border-radius: 6px;
      background: #fafafa;
      height: 420px;
      overflow: auto;
      padding: 0.7rem;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.88rem;
      line-height: 1.35;
    }}
    #devicebox {{
      margin-top: 1rem;
      border: 1px solid #bbb;
      border-radius: 6px;
      background: #f6fbff;
      height: 220px;
      overflow: auto;
      padding: 0.7rem;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.86rem;
      line-height: 1.35;
    }}
  </style>
</head>
<body>
  <h2>BSM Network Control</h2>
  <div class="panel">
    <div class="status">Status: <strong>{html.escape(state)}</strong></div>
    <div class="status">Force Upload: <strong>{html.escape(force_state)}</strong></div>
    {msg_html}
    <form method="post" action="/start">
      <button type="submit">Normal Ops</button>
    </form>
    <form method="post" action="/stop">
      <button type="submit">Stop Normal Ops</button>
    </form>
    {force_form_html}
    <div id="devicebox">Loading Arduino status...</div>
    <div id="logbox">Loading log...</div>
  </div>
  <script>
    const logbox = document.getElementById("logbox");
    const devicebox = document.getElementById("devicebox");
    const tzPreset = document.getElementById("tz_preset");
    const offsetInput = document.getElementById("offset_hours");
    async function refreshLog() {{
      try {{
        const resp = await fetch("/logs", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        const nearBottom = (logbox.scrollTop + logbox.clientHeight) >= (logbox.scrollHeight - 30);
        logbox.textContent = txt || "(No log output yet)";
        if (nearBottom) {{
          logbox.scrollTop = logbox.scrollHeight;
        }}
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}
    refreshLog();
    setInterval(refreshLog, 2000);

    async function refreshDevices() {{
      try {{
        const resp = await fetch("/devices", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        devicebox.textContent = txt || "(No device status yet)";
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}
    refreshDevices();
    setInterval(refreshDevices, 3000);

    if (tzPreset && offsetInput) {{
      tzPreset.addEventListener("change", () => {{
        const opt = tzPreset.options[tzPreset.selectedIndex];
        if (!opt) return;
        if (opt.value === "manual") return;
        const off = opt.getAttribute("data-offset");
        if (off !== null && off !== "") {{
          offsetInput.value = off;
        }}
      }});
    }}
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, body: bytes, code: int = HTTPStatus.OK) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, code: int = HTTPStatus.OK) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/logs":
            self._send_text(read_log_tail(MANAGER._log_path))
            return
        if self.path == "/devices":
            self._send_text(read_devices_status(Path("data/discovered_devices.csv")))
            return
        if self.path != "/":
            self._send_html(render_page("Not found."), HTTPStatus.NOT_FOUND)
            return
        self._send_html(render_page())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        form = parse_qs(body, keep_blank_values=True)
        if self.path == "/start":
            msg = MANAGER.start()
            self._send_html(render_page(msg))
            return
        if self.path == "/stop":
            msg = MANAGER.stop()
            self._send_html(render_page(msg))
            return
        if self.path == "/force-upload":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = MANAGER.start_force_upload(uid=uid, device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/query-time":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = query_device_time(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/set-time":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            preset = (form.get("tz_preset") or ["edt"])[0].strip().lower()
            if preset in TZ_PRESET_OFFSETS:
                offset_hours = TZ_PRESET_OFFSETS[preset]
            else:
                preset = "manual"
                offset_raw = (form.get("offset_hours") or [str(WEB_SET_TIME_OFFSET_HOURS)])[0].strip()
                try:
                    offset_hours = float(offset_raw)
                except ValueError:
                    self._send_html(render_page(f"Invalid offset_hours value: {offset_raw}"))
                    return
            set_last_set_time_state(offset_hours, preset)
            msg = set_device_time(device_ip=device_ip, offset_hours=offset_hours)
            self._send_html(render_page(msg))
            return
        if self.path == "/ping":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = ping_device(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/get-status":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = query_device_status(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/get-config":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = query_device_config(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/get-diag":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = query_device_diagnostics(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/get-last-data":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = query_last_data(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/set-config":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            start_hour_raw = (form.get("start_hour") or [""])[0].strip()
            end_hour_raw = (form.get("end_hour") or [""])[0].strip()
            updates = {}
            if start_hour_raw:
                try:
                    start_hour = int(start_hour_raw)
                    if 0 <= start_hour <= 23:
                        updates["START_HOUR"] = str(start_hour)
                    else:
                        self._send_html(render_page("START_HOUR must be 0-23"))
                        return
                except ValueError:
                    self._send_html(render_page("Invalid START_HOUR"))
                    return
            if end_hour_raw:
                try:
                    end_hour = int(end_hour_raw)
                    if 0 <= end_hour <= 23:
                        updates["END_HOUR"] = str(end_hour)
                    else:
                        self._send_html(render_page("END_HOUR must be 0-23"))
                        return
                except ValueError:
                    self._send_html(render_page("Invalid END_HOUR"))
                    return
            if not updates:
                self._send_html(render_page("No config updates specified"))
                return
            msg = set_device_config(device_ip=device_ip, config_updates=updates)
            self._send_html(render_page(msg))
            return
        if self.path == "/reboot":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = reboot_device(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/enter-data-mode":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = enter_data_mode(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        if self.path == "/clear-errors":
            raw = (form.get("device") or [""])[0]
            if "|" not in raw:
                self._send_html(render_page("Select an ONLINE Arduino first."))
                return
            uid, device_ip = raw.split("|", 1)
            if not uid or not device_ip:
                self._send_html(render_page("Invalid device selection."))
                return
            msg = clear_device_errors(device_ip=device_ip)
            self._send_html(render_page(msg))
            return
        self._send_html(render_page("Not found."), HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> int:
    host = "0.0.0.0"
    port = 5000
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"BSM web control ready: http://{host}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
