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
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bsm_network.config import (
    DEFAULT_DB_PATH,
    DEFAULT_DISCOVER_CSV,
    DEFAULT_DISCOVER_PORT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    build_force_upload_argv,
    build_normal_ops_argv,
    build_poll_now_argv,
    parse_args,
)
from bsm_network.db import is_transfer_active, read_devices_snapshot, set_burrow_id_by_short_uid
from bsm_network.discovery import run_discovery
from bsm_network.protocol import (
    clear_device_errors as protocol_clear_device_errors,
    enter_data_mode as protocol_enter_data_mode,
    get_device_config as protocol_get_device_config,
    get_device_diagnostics as protocol_get_device_diagnostics,
    get_device_status as protocol_get_device_status,
    get_last_data as protocol_get_last_data,
    ping_device as protocol_ping_device,
    reboot_device as protocol_reboot_device,
    set_device_config as protocol_set_device_config,
)

DISCOVER_CONTROL_PORT = DEFAULT_DISCOVER_PORT
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
    *build_normal_ops_argv(DEFAULT_DISCOVER_CSV),
]

POLL_NOW_ARGS = build_poll_now_argv(DEFAULT_DISCOVER_CSV)


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._log_path = Path("data/web_normal_ops.log")
        self._force_thread: threading.Thread | None = None
        self._force_task_id: int = 0
        self._force_active_id: int | None = None
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
            if self._force_thread is None:
                return False, None
            if not self._force_thread.is_alive():
                self._force_thread = None
                self._force_active_id = None
                return False, None
            return True, self._force_active_id

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
            if self._force_thread is not None and self._force_thread.is_alive():
                active_id = self._force_active_id if self._force_active_id is not None else 0
                return f"Force upload already running (task {active_id})."
            self._force_task_id += 1
            task_id = self._force_task_id
            self._force_active_id = task_id
            args_list = build_force_upload_argv(device_ip=device_ip, discover_csv=DEFAULT_DISCOVER_CSV)

        def _run_force_upload() -> None:
            self._force_log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().isoformat(timespec="seconds")
            with self._force_log_path.open("a", encoding="utf-8") as logf:
                logf.write(f"\n=== Force upload start {stamp} task={task_id} uid={uid} ip={device_ip} ===\n")
                logf.flush()
                rc = 1
                try:
                    args = parse_args(args_list)
                    with redirect_stdout(logf), redirect_stderr(logf):
                        rc = run_discovery(args)
                except Exception as exc:  # noqa: BLE001
                    logf.write(f"Force upload failed: {exc}\n")
                end_stamp = dt.datetime.now().isoformat(timespec="seconds")
                logf.write(f"=== Force upload end {end_stamp} task={task_id} rc={rc} ===\n")
            with self._lock:
                if self._force_active_id == task_id:
                    self._force_active_id = None

        thread = threading.Thread(target=_run_force_upload, name=f"force-upload-{task_id}", daemon=True)
        with self._lock:
            self._force_thread = thread
        thread.start()
        return f"Force upload started for {uid} ({device_ip}) (task {task_id})."

    def poll_now(self) -> str:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return "Stop Normal Ops before manual poll (port/bind conflict)."
            if self._force_thread is not None and self._force_thread.is_alive():
                return "Wait for force upload to finish before manual poll (port/bind conflict)."
            log_path = self._log_path

        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n=== Manual Poll start {stamp} ===\n")
            logf.flush()
            try:
                args = parse_args(POLL_NOW_ARGS)
                with redirect_stdout(logf), redirect_stderr(logf):
                    rc = run_discovery(args)
            except Exception as exc:  # noqa: BLE001
                logf.write(f"Manual poll failed to run: {exc}\n")
                return f"Manual poll failed: {exc}"
            end_stamp = dt.datetime.now().isoformat(timespec="seconds")
            logf.write(f"=== Manual Poll end {end_stamp} rc={rc} ===\n")
        if rc == 0:
            return "Manual poll completed. Device list refreshed."
        return f"Manual poll finished with non-zero status (rc={rc}). Check log output."

    def shutdown(self) -> None:
        self.stop()
        return


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
    lines.append("status   burrow_id      short_uid  unique_id                              ap_id       network_uid       device_ip      recv_ip        last_seen")
    lines.append("------   ------------   --------   ------------------------------------   ---------   ---------------   -----------   -----------    -------------------")
    for row in rows:
        status = row.get("status", "UNKNOWN")
        burrow_id = row.get("burrow_id", "")
        short_uid = row.get("short_uid", "")
        if str(row.get("short_uid_collision", "0")) in {"1", "true", "True"}:
            short_uid = f"{short_uid}*"
        uid = row.get("unique_id", "")
        ap_id = row.get("ap_id", "")
        net_uid = row.get("network_uid", "")
        dev_ip = row.get("device_ip", "")
        recv_ip = row.get("recv_ip", "")
        last_seen_raw = row.get("last_seen", "")
        lines.append(f"{status:<6}   {burrow_id:<12}   {short_uid:<8}   {uid:<36}   {ap_id:<9}   {net_uid:<15}   {dev_ip:<11}   {recv_ip:<11}    {last_seen_raw}")
    return "\n".join(lines)


def read_devices_rows(path: Path, online_seconds: int = 600) -> list[dict[str, str]]:
    db_rows = read_devices_snapshot(Path(DEFAULT_DB_PATH))
    if db_rows:
        rows = db_rows
    else:
        if not path.exists():
            return []

        rows = []
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
        line = protocol_ping_device(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        return f"PING OK from {device_ip}: {line}"
    except TimeoutError:
        return f"PING timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"PING failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_status(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        status = protocol_get_device_status(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in status.items())
        return f"GET_STATUS OK from {device_ip}: STATUS,{payload}"
    except TimeoutError:
        return f"GET_STATUS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_STATUS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_config(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        config = protocol_get_device_config(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in config.items())
        return f"GET_CONFIG OK from {device_ip}: CONFIG,{payload}"
    except TimeoutError:
        return f"GET_CONFIG timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_CONFIG failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_device_diagnostics(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        diag = protocol_get_device_diagnostics(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in diag.items())
        return f"GET_DIAGNOSTICS OK from {device_ip}: DIAG,{payload}"
    except TimeoutError:
        return f"GET_DIAGNOSTICS timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_DIAGNOSTICS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def query_last_data(device_ip: str, timeout_s: float = 2.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        data_dict = protocol_get_last_data(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        payload = ",".join(f"{k}={v}" for k, v in data_dict.items())
        return f"GET_LAST_DATA OK from {device_ip}: LAST_DATA,{payload}"
    except TimeoutError:
        return f"GET_LAST_DATA timeout from {device_ip} after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return f"GET_LAST_DATA failed for {device_ip}: {exc}"
    finally:
        sock.close()


def set_device_config(device_ip: str, config_updates: dict[str, str], timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_set_device_config(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            config_updates=config_updates,
            timeout_s=timeout_s,
        )
        if ok:
            return f"SET_CONFIG OK for {device_ip}"
        return f"SET_CONFIG failed/timeout from {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"SET_CONFIG failed for {device_ip}: {exc}"
    finally:
        sock.close()


def reboot_device(device_ip: str, timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_reboot_device(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if ok:
            return f"REBOOT OK for {device_ip}"
        return f"REBOOT failed/timeout for {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"REBOOT failed for {device_ip}: {exc}"
    finally:
        sock.close()


def enter_data_mode(device_ip: str, timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_enter_data_mode(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if ok:
            return f"ENTER_DATA_MODE OK for {device_ip}"
        return f"ENTER_DATA_MODE failed/timeout for {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"ENTER_DATA_MODE failed for {device_ip}: {exc}"
    finally:
        sock.close()


def can_enter_data_mode(uid: str) -> tuple[bool, str]:
    db_path = Path(DEFAULT_DB_PATH)
    try:
        active = is_transfer_active(db_path, uid)
    except Exception as exc:  # noqa: BLE001
        return False, f"Cannot verify transfer state for {uid}: {exc}"
    if active:
        return False, (
            f"ENTER_DATA_MODE blocked for {uid}: file transfer is in progress. "
            "Wait until transfer completes, then try again."
        )
    return True, ""


def assign_burrow_id(short_uid: str, burrow_id: str) -> str:
    db_path = Path(DEFAULT_DB_PATH)
    ok, msg = set_burrow_id_by_short_uid(db_path=db_path, short_uid=short_uid, burrow_id=burrow_id)
    return msg if ok else f"Assign burrow_id failed: {msg}"


def clear_device_errors(device_ip: str, timeout_s: float = 3.0) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        ok = protocol_clear_device_errors(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if ok:
            return f"CLEAR_ERRORS OK for {device_ip}"
        return f"CLEAR_ERRORS failed/timeout for {device_ip}"
    except Exception as exc:  # noqa: BLE001
        return f"CLEAR_ERRORS failed for {device_ip}: {exc}"
    finally:
        sock.close()


def render_page(message: str = "") -> bytes:
    running, pid = MANAGER.status()
    forcing, force_pid = MANAGER.force_status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    force_state = f"RUNNING (Task {force_pid})" if forcing else "IDLE"
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    online = [d for d in devices if d.get("status") == "ONLINE"]
    last_offset, last_preset = get_last_set_time_state()
    msg_html = f"<p><strong>{html.escape(message)}</strong></p>" if message else ""
    if devices:
        burrow_opts = []
        for d in sorted(devices, key=lambda x: ((x.get("short_uid", "") or ""), (x.get("unique_id", "") or ""))):
            uid = (d.get("unique_id", "") or "").strip()
            short_uid = (d.get("short_uid", "") or "").strip()
            if not short_uid:
                short_uid = uid[-6:] if len(uid) >= 6 else uid
            if str(d.get("short_uid_collision", "0")) in {"1", "true", "True"}:
                short_uid = f"{short_uid}*"
            burrow_id = (d.get("burrow_id", "") or "").strip()
            status = (d.get("status", "UNKNOWN") or "UNKNOWN").strip()
            label = f"{short_uid} | {uid} | burrow={burrow_id or '-'} | {status}"
            burrow_opts.append(
                f'<option value="{html.escape(short_uid.replace("*", ""))}">{html.escape(label)}</option>'
            )
        burrow_select_html = "\n".join(burrow_opts)
        burrow_form_html = f"""
    <form method="post" action="/assign-burrow-id" style="display:block; margin-top:0.75rem;">
      <label for="burrow_short_uid">Assign burrow_id by short_uid:</label>
      <select id="burrow_short_uid" name="short_uid" style="margin:0 0.5rem;">
        {burrow_select_html}
      </select>
      <label for="burrow_value">burrow_id:</label>
      <input id="burrow_value" name="burrow_id" type="text" maxlength="32" style="width:10rem; margin:0 0.5rem;" />
      <button type="submit">Save Burrow ID</button>
    </form>
"""
    else:
        burrow_form_html = '<p style="margin-top:0.75rem;"><em>No discovered devices available for burrow assignment.</em></p>'
    if online:
        opts = []
        for d in online:
            uid = d.get("unique_id", "")
            ip = d.get("device_ip", "") or d.get("recv_ip", "")
            burrow = (d.get("burrow_id", "") or "").strip()
            burrow_prefix = f"{burrow} | " if burrow else ""
            short_uid = (d.get("short_uid", "") or "").strip()
            if not short_uid:
                short_uid = uid[-6:]
            if str(d.get("short_uid_collision", "0")) in {"1", "true", "True"}:
                short_uid = f"{short_uid}*"
            label = f"{burrow_prefix}{short_uid} @ {ip} ({uid})"
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
    <form method="post" action="/poll-now">
      <button type="submit">Poll Now</button>
    </form>
    {burrow_form_html}
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
        if self.path == "/poll-now":
            msg = MANAGER.poll_now()
            self._send_html(render_page(msg))
            return
        if self.path == "/assign-burrow-id":
            short_uid = (form.get("short_uid") or [""])[0].strip().upper()
            burrow_id = (form.get("burrow_id") or [""])[0].strip()
            if not short_uid:
                self._send_html(render_page("short_uid is required for burrow assignment."))
                return
            msg = assign_burrow_id(short_uid=short_uid, burrow_id=burrow_id)
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
            ok, reason = can_enter_data_mode(uid=uid)
            if not ok:
                self._send_html(render_page(reason))
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
    host = DEFAULT_WEB_HOST
    port = DEFAULT_WEB_PORT
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
