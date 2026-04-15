#!/usr/bin/env python3
"""Minimal web UI to start/stop normal BSM operations."""

from __future__ import annotations

import html
import csv
import sqlite3
import socket
import subprocess
import sys
import threading
import datetime as dt
import time
from contextlib import redirect_stderr, redirect_stdout
from urllib.parse import parse_qs, urlparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bsm_network.config import (
    ACTIVE_NETWORK_PROFILE,
    ACTIVE_NETWORK_PROFILE_SOURCE,
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
from bsm_network.db import is_transfer_active, list_active_transfers, read_devices_snapshot, set_burrow_id_by_short_uid
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
ACTION_LOG_PATH = Path("data/web_actions.log")


def read_log_tail(path: Path, max_bytes: int = 120_000) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    return data.decode("utf-8", errors="replace")


def append_action_log(action: str, message: str) -> None:
    ACTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with ACTION_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {action}: {message}\n")


def read_activity_status() -> str:
    action_txt = read_log_tail(ACTION_LOG_PATH, max_bytes=80_000)
    normal_txt = read_log_tail(MANAGER._log_path, max_bytes=80_000)

    sections = []
    sections.append("=== WEB ACTIONS ===")
    sections.append(action_txt.strip() or "(No web actions yet)")
    sections.append("")
    sections.append("=== NORMAL OPS OUTPUT ===")
    sections.append(normal_txt.strip() or "(No normal-ops output yet)")
    return "\n".join(sections)


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


def read_today_uploads_status(db_path: Path) -> str:
    if not db_path.exists():
        return "(No upload DB yet)"

    today = dt.date.today().isoformat()
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT
                  COALESCE(t.burrow_id, ''),
                  COALESCE(d.short_uid, ''),
                  COALESCE(t.network_uid, ''),
                  COALESCE(t.source_filename, ''),
                  COALESCE(t.event_ts, '')
                FROM transfer_events t
                LEFT JOIN devices d
                  ON d.unique_id = t.unique_id
                WHERE t.status = 'saved'
                  AND date(substr(t.event_ts, 1, 10)) = ?
                ORDER BY t.event_ts DESC
                """,
                (today,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return f"(Upload list unavailable: {exc})"

    if not rows:
        return "(No files uploaded today)"

    lines = []
    lines.append("burrow_id      short_uid  network_uid       filename                uploaded_at")
    lines.append("------------   --------   ---------------   ----------------------  -------------------")
    for burrow_id, short_uid, network_uid, filename, uploaded_at in rows:
        lines.append(
            f"{str(burrow_id):<12}   {str(short_uid):<8}   {str(network_uid):<15}   "
            f"{str(filename):<22}  {str(uploaded_at):<19}"
        )
    return "\n".join(lines)


def _request_remote_file_list_with_sizes(device_ip: str, timeout_s: float = 8.0) -> tuple[list[tuple[str, int]], str]:
    transfer_id = f"LWEB{int(time.time() * 1000)}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.4)
    try:
        msg = f"LIST_FILES,{transfer_id}".encode("utf-8")
        sock.sendto(msg, (device_ip, DISCOVER_CONTROL_PORT))

        items: list[tuple[str, int]] = []
        seen: set[str] = set()
        got_end = False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data, (src_ip, _src_port) = sock.recvfrom(2048)
            except socket.timeout:
                continue
            if src_ip != device_ip:
                continue
            line = data.decode("utf-8", errors="replace").strip()
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2 or parts[1] != transfer_id:
                continue
            msg_type = parts[0]
            if msg_type == "ERROR":
                return [], f"Arduino error: {line}"
            if msg_type == "FILE_ITEM" and len(parts) >= 4:
                name = parts[2]
                try:
                    size = int(parts[3])
                except ValueError:
                    size = 0
                if name and name not in seen:
                    seen.add(name)
                    items.append((name, size))
                continue
            if msg_type == "FILE_LIST_END":
                got_end = True
                break
        if not got_end:
            return [], f"LIST_FILES timeout for {device_ip}"
        return items, ""
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    finally:
        sock.close()


def _read_uploaded_files_for_device(db_path: Path, unique_id: str) -> tuple[list[tuple[str, str]], str]:
    if not db_path.exists():
        return [], "(No upload DB yet)"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT COALESCE(source_filename, ''), COALESCE(event_ts, '')
                FROM transfer_events
                WHERE status = 'saved' AND unique_id = ?
                ORDER BY event_ts DESC
                """,
                (unique_id,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return [], f"(Upload history unavailable: {exc})"
    return [(str(r[0] or ""), str(r[1] or "")) for r in rows], ""


def _read_full_history_for_device(db_path: Path, unique_id: str) -> tuple[list[tuple[str, str, str]], str]:
    if not db_path.exists():
        return [], "(No SQLite DB yet)"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            cur = conn.execute(
                """
                SELECT event_ts, source, detail
                FROM (
                  SELECT
                    COALESCE(event_ts, '') AS event_ts,
                    'DISCOVERY' AS source,
                    ('status=' || COALESCE(status, '') || ' msg=' || COALESCE(message, '')) AS detail
                  FROM discovery_events
                  WHERE unique_id = ?

                  UNION ALL

                  SELECT
                    COALESCE(event_ts, '') AS event_ts,
                    'TRANSFER' AS source,
                    ('status=' || COALESCE(status, '') || ' file=' || COALESCE(source_filename, '') || ' msg=' || COALESCE(message, '')) AS detail
                  FROM transfer_events
                  WHERE unique_id = ?

                  UNION ALL

                  SELECT
                    COALESCE(event_ts, '') AS event_ts,
                    'SLOT' AS source,
                    ('action=' || COALESCE(action, '') || ' ap=' || COALESCE(ap_id, '') ||
                     ' total=' || COALESCE(CAST(running_total AS TEXT), '0') ||
                     ' on_ap=' || COALESCE(CAST(running_on_ap AS TEXT), '0')) AS detail
                  FROM slot_events
                  WHERE unique_id = ?
                )
                ORDER BY event_ts DESC
                """,
                (unique_id, unique_id, unique_id),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return [], f"(History unavailable: {exc})"

    out = [(str(r[0] or ""), str(r[1] or ""), str(r[2] or "")) for r in rows]
    return out, ""


def render_page(message: str = "") -> bytes:
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    msg_html = f"<p><strong>{html.escape(message)}</strong></p>" if message else ""
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Big Science Network</title>
  <style>
    :root {{
      --bg: #f2f4f7;
      --panel: #ffffff;
      --text: #1f2937;
      --line: #c7d2de;
      --primary: #0b5ea8;
      --accent: #e7f1fb;
    }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #f7fafc 0%, var(--bg) 100%);
      color: var(--text);
      font-family: "Avenir Next", "Trebuchet MS", sans-serif;
    }}
    .shell {{
      max-width: 1100px;
      margin: 1.25rem auto;
      padding: 0 1rem;
    }}
    .panel {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      padding: 1rem;
      box-shadow: 0 6px 20px rgba(23, 43, 77, 0.08);
    }}
    .title {{
      margin: 0 0 0.75rem 0;
      color: #0b2d4b;
      letter-spacing: 0.02em;
    }}
    .status {{
      margin: 0 0 0.75rem 0;
      font-weight: 600;
    }}
    .controls {{
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-bottom: 0.75rem;
    }}
    form {{ margin: 0; }}
    button {{
      border: 1px solid #2b6cb0;
      background: var(--primary);
      color: white;
      border-radius: 6px;
      padding: 0.55rem 0.9rem;
      font-size: 0.95rem;
      cursor: pointer;
    }}
    .placeholder-btn {{
      background: var(--accent);
      color: #0b2d4b;
      border-color: #8db4da;
    }}
    .section-title {{
      margin: 0.9rem 0 0.4rem 0;
      font-size: 0.95rem;
      color: #304a64;
      font-weight: 700;
    }}
    .scrollbox {{
      border: 1px solid var(--line);
      background: #fbfdff;
      border-radius: 6px;
      height: 260px;
      overflow: auto;
      padding: 0.65rem;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.84rem;
      line-height: 1.35;
    }}
    .nav-buttons {{
      margin-top: 0.9rem;
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}
    #activitybox {{
      margin-top: 0.8rem;
    }}
  </style>
</head>
<body>
  <div class="shell">
  <div class="panel">
    <h2 class="title">Big Science Network</h2>
    <div class="status">Status: <strong>{html.escape(state)}</strong></div>
    <div class="status">Profile: <strong>{html.escape(ACTIVE_NETWORK_PROFILE)}</strong> ({html.escape(ACTIVE_NETWORK_PROFILE_SOURCE)})</div>
    {msg_html}
    <div class="controls">
      <form method="post" action="/start">
        <button type="submit">Normal Ops</button>
      </form>
      <form method="post" action="/stop">
        <button type="submit">Stop Normal Ops</button>
      </form>
      <form method="post" action="/poll-now">
        <button type="submit">Poll Now</button>
      </form>
    </div>

    <div class="section-title">Known Arduinos</div>
    <div id="devicebox" class="scrollbox">Loading Arduino status...</div>

    <div class="section-title">Uploads Today</div>
    <div id="uploadsbox" class="scrollbox">Loading uploaded-file list...</div>

    <div class="nav-buttons">
      <form method="get" action="/file-transfers">
        <button type="submit" class="placeholder-btn">File Transfers</button>
      </form>
      <form method="get" action="/maintenance">
        <button type="submit" class="placeholder-btn">Maintenance</button>
      </form>
      <button type="button" class="placeholder-btn">Other</button>
    </div>

    <div class="section-title">Activity Log</div>
    <div id="activitybox" class="scrollbox">Loading activity output...</div>
  </div>
  </div>
  <script>
    const devicebox = document.getElementById("devicebox");
    const uploadsbox = document.getElementById("uploadsbox");
    const activitybox = document.getElementById("activitybox");
    async function refreshUploads() {{
      try {{
        const resp = await fetch("/uploads-today", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        uploadsbox.textContent = txt || "(No uploaded files today)";
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}
    refreshUploads();
    setInterval(refreshUploads, 3000);

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

    async function refreshActivity() {{
      try {{
        const resp = await fetch("/activity", {{ cache: "no-store" }});
        if (!resp.ok) {{
          return;
        }}
        const txt = await resp.text();
        const nearBottom = (activitybox.scrollTop + activitybox.clientHeight) >= (activitybox.scrollHeight - 30);
        activitybox.textContent = txt || "(No activity yet)";
        if (nearBottom) {{
          activitybox.scrollTop = activitybox.scrollHeight;
        }}
      }} catch (_err) {{
        // Keep last displayed text on transient fetch errors.
      }}
    }}
    refreshActivity();
    setInterval(refreshActivity, 2000);
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


def render_file_transfers_page(message: str = "", selected_uid: str = "") -> bytes:
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    msg_html = f"<p><strong>{html.escape(message)}</strong></p>" if message else ""
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    selected_uid = (selected_uid or "").strip()

    selected_device: dict[str, str] | None = None
    for d in devices:
        if (d.get("unique_id", "") or "").strip() == selected_uid:
            selected_device = d
            break

    device_rows_html = []
    for d in devices:
        uid = (d.get("unique_id", "") or "").strip()
        short_uid = (d.get("short_uid", "") or "").strip()
        if not short_uid:
            short_uid = uid[-6:] if len(uid) >= 6 else uid
        if str(d.get("short_uid_collision", "0")) in {"1", "true", "True"}:
            short_uid = f"{short_uid}*"
        burrow = (d.get("burrow_id", "") or "").strip() or "-"
        status = (d.get("status", "UNKNOWN") or "UNKNOWN").strip()
        ip = (d.get("device_ip", "") or d.get("recv_ip", "")).strip()
        checked = "checked" if uid == selected_uid else ""
        label = f"{status:<7} {burrow:<10} {short_uid:<8} {uid:<36} {ip}"
        device_rows_html.append(
            f'<label style="display:block; margin:0.15rem 0;"><input type="radio" name="uid" value="{html.escape(uid)}" {checked} /> '
            f'<span style="font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:0.84rem;">{html.escape(label)}</span></label>'
        )
    if not device_rows_html:
        device_rows_html = ['<div style="font-style:italic;">No devices discovered yet.</div>']

    selected_short = ""
    selected_ip = ""
    files_on_device_lines = ["(Select a known Arduino to view SD files)"]
    uploaded_lines = ["(Select a known Arduino to view upload history)"]
    history_lines = ["(Select a known Arduino to view complete DB history)"]
    active_rows: list[dict[str, str]] = []
    active_error = ""
    try:
        active_rows = list_active_transfers(Path(DEFAULT_DB_PATH))
    except Exception as exc:  # noqa: BLE001
        active_error = str(exc)
    active_busy = len(active_rows) > 0
    busy_note_html = ""
    if active_error:
        busy_note_html = f'<p><strong>Warning:</strong> Active-transfer check failed: {html.escape(active_error)}</p>'
    elif active_busy:
        status_lines = []
        status_lines.append("Active uploads currently running:")
        status_lines.append("short_uid  unique_id                              device_ip      file               started_at")
        status_lines.append("--------   ------------------------------------   -----------    ----------------   -------------------")
        by_uid = {str(d.get("unique_id", "")): d for d in devices}
        for row in active_rows:
            uid = row.get("unique_id", "")
            d = by_uid.get(uid, {})
            short_uid = (d.get("short_uid", "") or "").strip()
            if not short_uid:
                short_uid = uid[-6:] if len(uid) >= 6 else uid
            status_lines.append(
                f"{short_uid:<8}   {uid:<36}   {row.get('device_ip', ''):<11}    "
                f"{row.get('source_filename', ''):<16}   {row.get('started_at', '')}"
            )
        busy_note_html = (
            "<div class=\"busy-note\"><pre>"
            + html.escape("\n".join(status_lines))
            + "</pre></div>"
        )

    if selected_device is not None:
        selected_ip = (selected_device.get("device_ip", "") or selected_device.get("recv_ip", "")).strip()
        selected_short = (selected_device.get("short_uid", "") or "").strip()
        if not selected_short:
            selected_short = selected_uid[-6:] if len(selected_uid) >= 6 else selected_uid

        if active_busy:
            files_on_device_lines = [
                "(Live SD file query paused while uploads are active.)",
                "(Use 'Wait/Refresh' or 'Stop Normal Ops Safely', then reload.)",
            ]
        else:
            remote_items, remote_err = _request_remote_file_list_with_sizes(selected_ip, timeout_s=8.0)
            if remote_err:
                files_on_device_lines = [f"(Could not fetch files: {remote_err})"]
            else:
                files_on_device_lines = []
                files_on_device_lines.append("filename                          size_bytes")
                files_on_device_lines.append("--------------------------------  ----------")
                for name, size in sorted(remote_items, key=lambda x: x[0], reverse=True):
                    files_on_device_lines.append(f"{name:<32}  {size:>10}")
                if len(remote_items) == 0:
                    files_on_device_lines = ["(No files reported by Arduino)"]

        uploaded_rows, uploaded_err = _read_uploaded_files_for_device(Path(DEFAULT_DB_PATH), selected_uid)
        if uploaded_err:
            uploaded_lines = [uploaded_err]
        else:
            uploaded_lines = []
            uploaded_lines.append("filename                          uploaded_at")
            uploaded_lines.append("--------------------------------  -------------------")
            for name, ts in uploaded_rows:
                uploaded_lines.append(f"{name:<32}  {ts}")
            if len(uploaded_rows) == 0:
                uploaded_lines = ["(No uploaded files logged for this Arduino)"]

        history_rows, history_err = _read_full_history_for_device(Path(DEFAULT_DB_PATH), selected_uid)
        if history_err:
            history_lines = [history_err]
        else:
            history_lines = []
            history_lines.append("event_ts              source      detail")
            history_lines.append("-------------------  ----------  -----------------------------------------------")
            for ts, source, detail in history_rows:
                history_lines.append(f"{ts:<19}  {source:<10}  {detail}")
            if len(history_rows) == 0:
                history_lines = ["(No DB history for this Arduino)"]

    files_title_suffix = selected_short if selected_short else "..."
    device_rows_html_block = "".join(device_rows_html)
    files_on_device_block = "\n".join(html.escape(x) for x in files_on_device_lines)
    uploaded_block = "\n".join(html.escape(x) for x in uploaded_lines)
    history_block = "\n".join(html.escape(x) for x in history_lines)
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Big Science Network - File Transfers</title>
  <style>
    :root {{
      --bg: #f2f4f7;
      --panel: #ffffff;
      --text: #1f2937;
      --line: #c7d2de;
      --primary: #0b5ea8;
      --accent: #e7f1fb;
    }}
    body {{ margin: 0; background: linear-gradient(180deg, #f7fafc 0%, var(--bg) 100%); color: var(--text); font-family: "Avenir Next", "Trebuchet MS", sans-serif; }}
    .shell {{ max-width: 1180px; margin: 1.25rem auto; padding: 0 1rem; }}
    .panel {{ border: 1px solid var(--line); background: var(--panel); border-radius: 6px; padding: 1rem; box-shadow: 0 6px 20px rgba(23, 43, 77, 0.08); }}
    .title {{ margin: 0; color: #0b2d4b; letter-spacing: 0.02em; }}
    .subtitle {{ margin: 0.25rem 0 0.8rem 0; color: #304a64; font-weight: 700; }}
    .status {{ margin: 0 0 0.75rem 0; font-weight: 600; }}
    .controls {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.75rem; }}
    form {{ margin: 0; }}
    button {{ border: 1px solid #2b6cb0; background: var(--primary); color: white; border-radius: 6px; padding: 0.55rem 0.9rem; font-size: 0.95rem; cursor: pointer; }}
    .section-title {{ margin: 0.9rem 0 0.4rem 0; font-size: 0.95rem; color: #304a64; font-weight: 700; }}
    .scrollbox {{ border: 1px solid var(--line); background: #fbfdff; border-radius: 6px; height: 260px; overflow: auto; padding: 0.65rem; white-space: pre; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.84rem; line-height: 1.35; }}
    .grid2 {{ margin-top: 0.8rem; display: grid; gap: 0.8rem; grid-template-columns: 1fr 1fr; }}
    .busy-note {{
      margin: 0.3rem 0 0.8rem 0;
      border: 1px solid #e5b97a;
      background: #fff4dd;
      border-radius: 6px;
      padding: 0.5rem;
    }}
    .busy-note pre {{
      margin: 0;
      white-space: pre;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.82rem;
      line-height: 1.3;
      color: #6e4b00;
    }}
    @media (max-width: 900px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="panel">
      <h2 class="title">Big Science Network</h2>
      <div class="subtitle">File Transfers</div>
      <div class="status">Status: <strong>{html.escape(state)}</strong></div>
      <div class="status">Profile: <strong>{html.escape(ACTIVE_NETWORK_PROFILE)}</strong> ({html.escape(ACTIVE_NETWORK_PROFILE_SOURCE)})</div>
      {msg_html}
      <div class="controls">
        <form method="get" action="/"><button type="submit">Dashboard</button></form>
        <form method="get" action="/file-transfers">
          <input type="hidden" name="uid" value="{html.escape(selected_uid)}" />
          <button type="submit">Wait/Refresh</button>
        </form>
        <form method="post" action="/file-transfers-stop-safe">
          <input type="hidden" name="uid" value="{html.escape(selected_uid)}" />
          <button type="submit">Stop Normal Ops Safely</button>
        </form>
      </div>
      {busy_note_html}

      <div class="section-title">Known Arduinos (select one)</div>
      <form method="get" action="/file-transfers">
        <div class="scrollbox" style="white-space: normal;">
          {device_rows_html_block}
        </div>
        <div style="margin-top:0.5rem;">
          <button type="submit">Load File Lists</button>
        </div>
      </form>

      <div class="grid2">
        <div>
          <div class="section-title">Files on {html.escape(files_title_suffix)}</div>
          <div class="scrollbox">{files_on_device_block}</div>
        </div>
        <div>
          <div class="section-title">Files uploaded from {html.escape(files_title_suffix)}</div>
          <div class="scrollbox">{uploaded_block}</div>
        </div>
      </div>

      <div class="section-title">Complete SQLite History for {html.escape(files_title_suffix)}</div>
      <div class="scrollbox">{history_block}</div>
    </div>
  </div>
</body>
</html>
"""
    return page.encode("utf-8")


def _find_device_by_uid(devices: list[dict[str, str]], selected_uid: str) -> dict[str, str] | None:
    for d in devices:
        if (d.get("unique_id", "") or "").strip() == selected_uid:
            return d
    return None


def _build_device_select_rows(devices: list[dict[str, str]], selected_uid: str) -> str:
    rows = []
    for d in devices:
        uid = (d.get("unique_id", "") or "").strip()
        short_uid = (d.get("short_uid", "") or "").strip()
        if not short_uid:
            short_uid = uid[-6:] if len(uid) >= 6 else uid
        if str(d.get("short_uid_collision", "0")) in {"1", "true", "True"}:
            short_uid = f"{short_uid}*"
        burrow = (d.get("burrow_id", "") or "").strip() or "-"
        status = (d.get("status", "UNKNOWN") or "UNKNOWN").strip()
        ip = (d.get("device_ip", "") or d.get("recv_ip", "")).strip()
        checked = "checked" if uid == selected_uid else ""
        label = f"{status:<7} {burrow:<10} {short_uid:<8} {uid:<36} {ip}"
        rows.append(
            f'<label style="display:block; margin:0.15rem 0;"><input type="radio" name="uid" value="{html.escape(uid)}" {checked} /> '
            f'<span style="font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:0.84rem;">{html.escape(label)}</span></label>'
        )
    if not rows:
        return '<div style="font-style:italic;">No devices discovered yet.</div>'
    return "".join(rows)


def _maintenance_info_lines(device_ip: str) -> list[str]:
    status = query_device_status(device_ip=device_ip)
    config = query_device_config(device_ip=device_ip)
    diag = query_device_diagnostics(device_ip=device_ip)
    rtc = query_device_time(device_ip=device_ip)
    lines = []
    lines.append("Maintenance Info     | Value")
    lines.append("-------------------- | -------------------------------------------------------------")
    lines.append(f"Get Status           | {status}")
    lines.append(f"Get Config           | {config}")
    lines.append(f"Get Diagnostics      | {diag}")
    lines.append(f"Get RTC Time         | {rtc}")
    return lines


def _rtc_panel_lines(device_ip: str, timeout_s: float = 2.0) -> tuple[str, str, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(b"GET_TIME", (device_ip, DISCOVER_CONTROL_PORT))
        data, (src_ip, _src_port) = sock.recvfrom(2048)
        line = data.decode("utf-8", errors="replace").strip()
        if src_ip != device_ip:
            return "result", f"unexpected_source={src_ip}"
        if line.startswith("TIME,"):
            parts = line.split(",", 2)
            epoch = parts[1] if len(parts) > 1 else ""
            ts = parts[2] if len(parts) > 2 else ""
            return _format_two_line_columns([("epoch", epoch), ("timestamp", ts)])
        return "result", "------", line
    except socket.timeout:
        return "result", "------", f"timeout after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return "result", "------", f"error: {exc}"
    finally:
        sock.close()


def _dict_panel_lines(
    fetch_fn,
    device_ip: str,
    fallback_order: list[str],
    timeout_s: float = 2.0,
) -> tuple[str, str, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        data = fetch_fn(
            control_sock=sock,
            device_ip=device_ip,
            control_port=DISCOVER_CONTROL_PORT,
            timeout_s=timeout_s,
        )
        if not data:
            return "result", "------", "no data"
        keys = [k for k in fallback_order if k in data] + [k for k in data.keys() if k not in fallback_order]
        cols = [(k, str(data.get(k, ""))) for k in keys]
        return _format_two_line_columns(cols) if cols else ("result", "------", "ok")
    except TimeoutError:
        return "result", "------", f"timeout after {timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        return "result", "------", f"error: {exc}"
    finally:
        sock.close()


def _maintenance_panel_data(device_ip: str) -> dict[str, tuple[str, str, str]]:
    rtc = _rtc_panel_lines(device_ip=device_ip)
    status = _dict_panel_lines(
        fetch_fn=protocol_get_device_status,
        device_ip=device_ip,
        fallback_order=["UPTIME", "MODE", "SD_FREE_KB", "LAST_DATA_TS", "BATTERY"],
        timeout_s=2.0,
    )
    config = _dict_panel_lines(
        fetch_fn=protocol_get_device_config,
        device_ip=device_ip,
        fallback_order=["START_HOUR", "END_HOUR", "DEVICE_ID"],
        timeout_s=2.0,
    )
    diagnostics = _dict_panel_lines(
        fetch_fn=protocol_get_device_diagnostics,
        device_ip=device_ip,
        fallback_order=["RTC_OK", "RTC_ERRORS", "I2C_ERRORS", "SD_ERRORS"],
        timeout_s=2.0,
    )
    return {
        "RTC Time": rtc,
        "Status": status,
        "Config": config,
        "Diagnostics": diagnostics,
    }


def _format_two_line_columns(cols: list[tuple[str, str]]) -> tuple[str, str, str]:
    if not cols:
        return "result", "------", ""
    # Add one trailing space to every column width so columns are separated by one space.
    widths = [max(len(h), len(v)) + 1 for h, v in cols]
    header = "".join(h.ljust(widths[i]) for i, (h, _v) in enumerate(cols)).rstrip()
    # Per user spec: dashes per column use (column width - 1), preserving an inter-column space.
    separator = "".join((("-" * max(1, widths[i] - 1)).ljust(widths[i])) for i in range(len(cols))).rstrip()
    values = "".join(v.ljust(widths[i]) for i, (_h, v) in enumerate(cols)).rstrip()
    return header, separator, values


def _mini_panel_block(header: str, separator: str, values: str) -> str:
    # Build a dashboard-style 3-line block:
    # header row
    # dashed separator row
    # value row
    return f"{header}\n{separator}\n{values}"


def render_maintenance_page(message: str = "", selected_uid: str = "") -> bytes:
    running, pid = MANAGER.status()
    state = f"RUNNING (PID {pid})" if running else "STOPPED"
    msg_html = f"<p><strong>{html.escape(message)}</strong></p>" if message else ""
    devices = read_devices_rows(Path("data/discovered_devices.csv"))
    selected_uid = (selected_uid or "").strip()
    selected_device = _find_device_by_uid(devices, selected_uid)

    panels: dict[str, tuple[str, str, str]] = {
        "RTC Time": ("result", "------", "select a known Arduino"),
        "Status": ("result", "------", "select a known Arduino"),
        "Config": ("result", "------", "select a known Arduino"),
        "Diagnostics": ("result", "------", "select a known Arduino"),
    }
    selected_short = "..."
    if selected_device is not None:
        uid = (selected_device.get("unique_id", "") or "").strip()
        selected_short = (selected_device.get("short_uid", "") or "").strip()
        if not selected_short:
            selected_short = uid[-6:] if len(uid) >= 6 else uid
        device_ip = (selected_device.get("device_ip", "") or selected_device.get("recv_ip", "")).strip()
        panels = _maintenance_panel_data(device_ip=device_ip)
    device_rows_html_block = _build_device_select_rows(devices, selected_uid)

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Big Science Network - Maintenance</title>
  <style>
    :root {{
      --bg: #f2f4f7;
      --panel: #ffffff;
      --text: #1f2937;
      --line: #c7d2de;
      --primary: #0b5ea8;
    }}
    body {{ margin: 0; background: linear-gradient(180deg, #f7fafc 0%, var(--bg) 100%); color: var(--text); font-family: "Avenir Next", "Trebuchet MS", sans-serif; }}
    .shell {{ max-width: 1180px; margin: 1.25rem auto; padding: 0 1rem; }}
    .panel {{ border: 1px solid var(--line); background: var(--panel); border-radius: 6px; padding: 1rem; box-shadow: 0 6px 20px rgba(23, 43, 77, 0.08); }}
    .title {{ margin: 0; color: #0b2d4b; letter-spacing: 0.02em; }}
    .subtitle {{ margin: 0.25rem 0 0.8rem 0; color: #304a64; font-weight: 700; }}
    .status {{ margin: 0 0 0.75rem 0; font-weight: 600; }}
    .controls {{ display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.75rem; }}
    form {{ margin: 0; }}
    button {{ border: 1px solid #2b6cb0; background: var(--primary); color: white; border-radius: 6px; padding: 0.55rem 0.9rem; font-size: 0.95rem; cursor: pointer; }}
    .section-title {{ margin: 0.9rem 0 0.4rem 0; font-size: 0.95rem; color: #304a64; font-weight: 700; }}
    .scrollbox {{ border: 1px solid var(--line); background: #fbfdff; border-radius: 6px; height: 260px; overflow: auto; padding: 0.65rem; white-space: pre; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.84rem; line-height: 1.35; }}
    .mini-grid {{ margin-top: 0.9rem; display: grid; gap: 0.8rem; grid-template-columns: 1fr 1fr; }}
    .mini-title {{ margin: 0 0 0.25rem 0; font-size: 0.9rem; color: #304a64; font-weight: 700; }}
    .mini-box {{ border: 1px solid var(--line); background: #fbfdff; border-radius: 6px; height: 88px; overflow: auto; padding: 0.55rem; white-space: pre; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.84rem; line-height: 1.3; }}
    @media (max-width: 900px) {{ .mini-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="panel">
      <h2 class="title">Big Science Network</h2>
      <div class="subtitle">Maintenance</div>
      <div class="status">Status: <strong>{html.escape(state)}</strong></div>
      <div class="status">Profile: <strong>{html.escape(ACTIVE_NETWORK_PROFILE)}</strong> ({html.escape(ACTIVE_NETWORK_PROFILE_SOURCE)})</div>
      {msg_html}

      <div class="controls">
        <form method="get" action="/"><button type="submit">Dashboard</button></form>
      </div>

      <div class="section-title">Known Arduinos (select one)</div>
      <form method="get" action="/maintenance">
        <div class="scrollbox" style="white-space: normal;">
          {device_rows_html_block}
        </div>
        <div style="margin-top:0.5rem;">
          <button type="submit">Load Maintenance Info</button>
        </div>
      </form>

      <div class="controls" style="margin-top:0.8rem;">
        <form method="post" action="/maintenance-action">
          <input type="hidden" name="uid" value="{html.escape(selected_uid)}" />
          <input type="hidden" name="action" value="set-time" />
          <button type="submit">Set RTC Time</button>
        </form>
        <form method="post" action="/maintenance-action">
          <input type="hidden" name="uid" value="{html.escape(selected_uid)}" />
          <input type="hidden" name="action" value="ping" />
          <button type="submit">Ping</button>
        </form>
        <form method="post" action="/maintenance-action">
          <input type="hidden" name="uid" value="{html.escape(selected_uid)}" />
          <input type="hidden" name="action" value="reboot" />
          <button type="submit">Reboot</button>
        </form>
      </div>

      <div class="section-title">Maintenance Info for {html.escape(selected_short)}</div>
      <div class="mini-grid">
        <div>
          <div class="mini-title">RTC Time</div>
          <div class="mini-box">{html.escape(_mini_panel_block(panels["RTC Time"][0], panels["RTC Time"][1], panels["RTC Time"][2]))}</div>
        </div>
        <div>
          <div class="mini-title">Status</div>
          <div class="mini-box">{html.escape(_mini_panel_block(panels["Status"][0], panels["Status"][1], panels["Status"][2]))}</div>
        </div>
        <div>
          <div class="mini-title">Config</div>
          <div class="mini-box">{html.escape(_mini_panel_block(panels["Config"][0], panels["Config"][1], panels["Config"][2]))}</div>
        </div>
        <div>
          <div class="mini-title">Diagnostics</div>
          <div class="mini-box">{html.escape(_mini_panel_block(panels["Diagnostics"][0], panels["Diagnostics"][1], panels["Diagnostics"][2]))}</div>
        </div>
      </div>
    </div>
  </div>
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
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if route == "/devices":
            self._send_text(read_devices_status(Path("data/discovered_devices.csv")))
            return
        if route == "/uploads-today":
            self._send_text(read_today_uploads_status(Path(DEFAULT_DB_PATH)))
            return
        if route == "/activity":
            self._send_text(read_activity_status())
            return
        if route == "/logs":
            self._send_text(read_log_tail(MANAGER._log_path))
            return
        if route == "/file-transfers":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_html(render_file_transfers_page(selected_uid=selected_uid))
            return
        if route == "/maintenance":
            selected_uid = (query.get("uid") or [""])[0].strip()
            self._send_html(render_maintenance_page(selected_uid=selected_uid))
            return
        if route != "/":
            self._send_html(render_page("Not found."), HTTPStatus.NOT_FOUND)
            return
        self._send_html(render_page())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        form = parse_qs(body, keep_blank_values=True)
        if self.path == "/start":
            msg = MANAGER.start()
            append_action_log("start", msg)
            self._send_html(render_page(msg))
            return
        if self.path == "/stop":
            msg = MANAGER.stop()
            append_action_log("stop", msg)
            self._send_html(render_page(msg))
            return
        if self.path == "/poll-now":
            msg = MANAGER.poll_now()
            append_action_log("poll-now", msg)
            self._send_html(render_page(msg))
            return
        if self.path == "/file-transfers-stop-safe":
            selected_uid = (form.get("uid") or [""])[0].strip()
            msg = MANAGER.stop()
            append_action_log("stop-for-file-transfers", msg)
            self._send_html(render_file_transfers_page(message=msg, selected_uid=selected_uid))
            return
        if self.path == "/maintenance-action":
            selected_uid = (form.get("uid") or [""])[0].strip()
            action = (form.get("action") or [""])[0].strip()
            devices = read_devices_rows(Path("data/discovered_devices.csv"))
            selected_device = _find_device_by_uid(devices, selected_uid)
            if selected_device is None:
                self._send_html(render_maintenance_page(message="Select a known Arduino first.", selected_uid=selected_uid))
                return
            device_ip = (selected_device.get("device_ip", "") or selected_device.get("recv_ip", "")).strip()
            if not device_ip:
                self._send_html(render_maintenance_page(message="Selected Arduino has no IP address.", selected_uid=selected_uid))
                return
            if action == "set-time":
                msg = set_device_time(device_ip=device_ip, offset_hours=WEB_SET_TIME_OFFSET_HOURS)
                append_action_log("maintenance-set-time", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return
            if action == "ping":
                msg = ping_device(device_ip=device_ip)
                append_action_log("maintenance-ping", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return
            if action == "reboot":
                msg = reboot_device(device_ip=device_ip)
                append_action_log("maintenance-reboot", msg)
                self._send_html(render_maintenance_page(message=msg, selected_uid=selected_uid))
                return
            self._send_html(render_maintenance_page(message=f"Unknown maintenance action: {action}", selected_uid=selected_uid))
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
