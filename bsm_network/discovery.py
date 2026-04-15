from __future__ import annotations

import argparse
import json
import datetime as dt
import ipaddress
import re
import socket
import subprocess
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .db import (
    find_unique_ids_by_short_uid,
    init_db,
    log_discovery_event,
    log_slot_event,
    log_transfer_event,
    upsert_device,
)
from .protocol import parse_payload, request_remote_file_list, send_time_sync, transfer_file_protocol
from .records import (
    build_local_filename,
    ensure_unique_filename,
    load_received_filenames,
    save_device_data_csv,
    select_most_recent_file,
    select_most_recent_unsaved_file,
    write_discovery_csv,
)

def _resolve_target_yymmdd(args: argparse.Namespace) -> str | None:
    mode = (getattr(args, "file_day", "yesterday") or "yesterday").lower()
    if mode == "latest":
        return None

    now_ref = dt.datetime.utcnow() + dt.timedelta(hours=float(args.time_offset_hours))
    if mode == "today":
        target = now_ref
    else:
        target = now_ref - dt.timedelta(days=1)
    return target.strftime("%y%m%d")


def detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def detect_broadcast_ip(bind_ip: str) -> str:
    target_ip = bind_ip if bind_ip and bind_ip != "0.0.0.0" else detect_lan_ip()
    if target_ip == "127.0.0.1":
        return "255.255.255.255"

    try:
        out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return "255.255.255.255"

    inet_re = re.compile(
        r"\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+0x([0-9a-fA-F]+)(?:\s+broadcast\s+(\d+\.\d+\.\d+\.\d+))?"
    )
    for line in out.splitlines():
        m = inet_re.match(line)
        if not m:
            continue
        ip = m.group(1)
        if ip != target_ip:
            continue

        broadcast = m.group(3)
        if broadcast:
            return broadcast

        try:
            netmask_hex = m.group(2)
            netmask_int = int(netmask_hex, 16)
            netmask = str(ipaddress.IPv4Address(netmask_int))
            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
            return str(network.broadcast_address)
        except ValueError:
            return "255.255.255.255"

    return "255.255.255.255"


def parse_id_response(payload: str) -> dict[str, str | int] | None:
    if not payload.startswith("ID,"):
        return None

    parts = [p.strip() for p in payload.split(",")]
    if len(parts) < 2:
        return None

    uid = parts[1]
    if not uid:
        return None

    device_ip = parts[2] if len(parts) > 2 else ""
    udp_target_ip = parts[3] if len(parts) > 3 else ""
    udp_target_port_raw = parts[4] if len(parts) > 4 else ""
    try:
        udp_target_port = int(udp_target_port_raw) if udp_target_port_raw else 0
    except ValueError:
        udp_target_port = 0

    return {
        "unique_id": uid,
        "device_ip": device_ip,
        "udp_target_ip": udp_target_ip,
        "udp_target_port": udp_target_port,
        "network_uid": parts[5] if len(parts) > 5 else "",
    }


def parse_net_uid_response(payload: str) -> dict[str, str] | None:
    if not payload.startswith("NET_UID,"):
        return None
    parts = [p.strip() for p in payload.split(",")]
    if len(parts) < 2:
        return None
    network_uid = parts[1]
    if not network_uid:
        return None

    network_hostname = ""
    wifi_mac = ""
    for token in parts[2:]:
        if token.startswith("HOST="):
            network_hostname = token[5:]
        elif token.startswith("MAC="):
            wifi_mac = token[4:]
    return {
        "network_uid": network_uid,
        "network_hostname": network_hostname,
        "wifi_mac": wifi_mac,
    }


def request_network_uid(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float = 1.0,
) -> dict[str, str] | None:
    original_timeout = control_sock.gettimeout()
    try:
        control_sock.settimeout(timeout_s)
        control_sock.sendto(b"GET_NET_UID", (device_ip, control_port))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data, (src_ip, _src_port) = control_sock.recvfrom(2048)
            except socket.timeout:
                return None
            if src_ip != device_ip:
                continue
            payload = data.decode("utf-8", errors="replace").strip()
            parsed = parse_net_uid_response(payload)
            if parsed is not None:
                return parsed
        return None
    finally:
        control_sock.settimeout(original_timeout)


def collect_device_lines(
    sock: socket.socket,
    device: dict[str, str | int],
    args: argparse.Namespace,
) -> list[dict[str, str | int]]:
    uid = str(device["unique_id"])
    device_ip = str(device["device_ip"] or device["recv_ip"])

    sock.sendto(args.download_command.encode("utf-8"), (device_ip, args.discover_port))
    print(f"Requesting {args.download_lines} line(s) from {uid} at {device_ip}:{args.discover_port}")

    rows: list[dict[str, str | int]] = []
    deadline = time.monotonic() + args.download_timeout
    while time.monotonic() < deadline and len(rows) < args.download_lines:
        try:
            data, (src_ip, src_port) = sock.recvfrom(args.buffer_size)
        except socket.timeout:
            continue

        payload = data.decode("utf-8", errors="replace").strip()
        parsed = parse_payload(payload)
        if parsed is None:
            continue

        if str(parsed["device_id"]) != uid:
            continue

        rows.append(
            {
                "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
                "unique_id": uid,
                "device_ip": device_ip,
                "src_ip": src_ip,
                "src_port": src_port,
                "unix_time": parsed["unix_time"],
                "sample": parsed["sample"],
            }
        )

    return rows


def _short_uid_from_unique_id(full_id: str, out_len: int = 6) -> str:
    data = (full_id or "").strip()
    if not data:
        return ""

    h = 2166136261  # FNV-1a offset
    for b in data.encode("utf-8", errors="ignore"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF  # FNV-1a prime

    # Extra avalanche to improve diffusion.
    h ^= (h >> 16)
    h = (h * 0x7FEB352D) & 0xFFFFFFFF
    h ^= (h >> 15)
    h = (h * 0x846CA68B) & 0xFFFFFFFF
    h ^= (h >> 16)

    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    out = []
    x = h & 0xFFFFFFFF
    for _ in range(max(1, out_len)):
        out.append(alphabet[x & 31])
        x = ((x >> 5) ^ ((x << 27) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return "".join(out)


def _normalize_ap_id(value: str | None) -> str:
    token = (value or "").strip()
    return token if token else "DEFAULT"


def _normalize_map_key(value: str | None) -> str:
    return (value or "").strip().upper()


def _load_json_config(path_value: str) -> dict:
    path = Path(path_value).expanduser()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return data


def _extract_device_mapping(data: dict) -> dict[str, str]:
    raw = data.get("device_to_ap")
    if raw is None:
        raw = data.get("mappings", {})
    if not isinstance(raw, dict):
        return {}

    out: dict[str, str] = {}
    for k, v in raw.items():
        key = _normalize_map_key(str(k))
        ap = _normalize_ap_id(str(v))
        if key:
            out[key] = ap
    return out


def _extract_ap_limits(data: dict) -> dict[str, int]:
    raw = data.get("ap_limits", {})
    if not isinstance(raw, dict):
        return {}

    out: dict[str, int] = {}
    for k, v in raw.items():
        ap = _normalize_ap_id(str(k))
        try:
            limit = int(v)
        except (TypeError, ValueError):
            continue
        if limit > 0:
            out[ap] = limit
    return out


def _extract_burrow_mapping(data: dict) -> dict[str, str]:
    out: dict[str, str] = {}

    raw_simple = data.get("device_to_burrow", {})
    if isinstance(raw_simple, dict):
        for k, v in raw_simple.items():
            key = _normalize_map_key(str(k))
            burrow_id = str(v).strip()
            if key and burrow_id:
                out[key] = burrow_id

    raw_meta = data.get("device_meta", {})
    if isinstance(raw_meta, dict):
        for k, meta in raw_meta.items():
            key = _normalize_map_key(str(k))
            if not key or not isinstance(meta, dict):
                continue
            burrow_id = str(meta.get("burrow_id", "")).strip()
            if burrow_id:
                out[key] = burrow_id
    return out


def _build_ap_routing_config(args: argparse.Namespace) -> tuple[dict[str, str], dict[str, int], str, dict[str, str]]:
    default_ap = _normalize_ap_id(getattr(args, "default_ap_id", "DEFAULT"))
    default_limit = max(1, int(getattr(args, "default_ap_limit", 1)))

    merged_mapping: dict[str, str] = {}
    merged_limits: dict[str, int] = {}
    merged_burrow: dict[str, str] = {}

    for label, path_value in (("network-map", args.network_map), ("runtime-ap-map", args.runtime_ap_map)):
        if not path_value:
            continue
        try:
            data = _load_json_config(path_value)
        except Exception as exc:
            print(f"Warning: failed to load {label} '{path_value}': {exc}")
            continue

        file_default_ap = _normalize_ap_id(str(data.get("default_ap", "") or ""))
        if file_default_ap and file_default_ap != "DEFAULT":
            default_ap = file_default_ap
        merged_limits.update(_extract_ap_limits(data))
        merged_mapping.update(_extract_device_mapping(data))
        merged_burrow.update(_extract_burrow_mapping(data))

    if default_ap not in merged_limits:
        merged_limits[default_ap] = default_limit
    if "DEFAULT" not in merged_limits:
        merged_limits["DEFAULT"] = default_limit

    return merged_mapping, merged_limits, default_ap, merged_burrow


def _resolve_ap_for_device(
    row: dict[str, str | int],
    device_to_ap: dict[str, str],
    default_ap: str,
) -> tuple[str, str]:
    for key in (
        _normalize_map_key(str(row.get("unique_id", ""))),
        _normalize_map_key(str(row.get("network_uid", ""))),
        _normalize_map_key(str(row.get("device_ip", ""))),
        _normalize_map_key(str(row.get("recv_ip", ""))),
    ):
        if not key:
            continue
        ap = device_to_ap.get(key)
        if ap:
            return ap, "mapping"
    return default_ap, "default"


def _resolve_burrow_for_device(
    row: dict[str, str | int],
    device_to_burrow: dict[str, str],
) -> str:
    for key in (
        _normalize_map_key(str(row.get("unique_id", ""))),
        _normalize_map_key(str(row.get("network_uid", ""))),
        _normalize_map_key(str(row.get("device_ip", ""))),
        _normalize_map_key(str(row.get("recv_ip", ""))),
    ):
        if not key:
            continue
        burrow_id = device_to_burrow.get(key, "").strip()
        if burrow_id:
            return burrow_id
    return ""


class APSlotManager:
    def __init__(self, ap_limits: dict[str, int], global_limit: int):
        self.ap_limits = ap_limits
        self.global_limit = max(1, global_limit)
        self.running_by_ap: dict[str, int] = {}
        self.running_total = 0
        self._cond = threading.Condition(threading.Lock())

    def acquire(self, ap_id: str) -> None:
        limit = max(1, self.ap_limits.get(ap_id, self.ap_limits.get("DEFAULT", 1)))
        with self._cond:
            while self.running_total >= self.global_limit or self.running_by_ap.get(ap_id, 0) >= limit:
                self._cond.wait()
            self.running_total += 1
            self.running_by_ap[ap_id] = self.running_by_ap.get(ap_id, 0) + 1
            if self.running_by_ap[ap_id] > limit:
                print(f"Warning: AP slot invariant exceeded for {ap_id}: {self.running_by_ap[ap_id]} > {limit}")

    def release(self, ap_id: str) -> None:
        with self._cond:
            if self.running_total > 0:
                self.running_total -= 1
            cur = self.running_by_ap.get(ap_id, 0)
            if cur > 0:
                self.running_by_ap[ap_id] = cur - 1
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, dict[str, int]]:
        with self._cond:
            return self.running_total, dict(self.running_by_ap)


def _open_device_control_socket(bind_ip: str) -> socket.socket:
    requested_bind = (bind_ip or "").strip() or "0.0.0.0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((requested_bind, 0))
    except OSError:
        if requested_bind != "0.0.0.0":
            sock.bind(("0.0.0.0", 0))
        else:
            raise
    sock.settimeout(0.2)
    return sock


def _transfer_latest_file_for_device(
    row: dict[str, str | int],
    args: argparse.Namespace,
    bind_ip: str,
    target_yymmdd: str | None,
    file_output_root: Path,
    file_log_root: Path,
) -> dict[str, str | float]:
    started = time.monotonic()
    uid = str(row["unique_id"])
    short_uid = str(row.get("short_uid", "")).strip().upper()
    display_id = short_uid if short_uid else (uid[-6:] if len(uid) >= 6 else uid).upper()
    device_ip = str(row["device_ip"] or row["recv_ip"])
    ap_id = str(row.get("ap_id", "DEFAULT"))
    network_uid = str(row.get("network_uid", ""))
    burrow_id = str(row.get("burrow_id", ""))

    base_result: dict[str, str | float] = {
        "unique_id": uid,
        "network_uid": network_uid,
        "burrow_id": burrow_id,
        "ap_id": ap_id,
        "device_ip": device_ip,
        "source_filename": "",
        "saved_path": "",
        "status": "error",
        "message": "",
        "error_text": "",
        "duration_s": 0.0,
    }

    control_sock = _open_device_control_socket(bind_ip)
    try:
        if args.sync_time:
            sync_epoch = int(time.time() + (args.time_offset_hours * 3600.0))
            synced = send_time_sync(
                control_sock=control_sock,
                device_ip=device_ip,
                control_port=args.discover_port,
                epoch=sync_epoch,
                timeout_s=3.0,
            )
            if synced:
                print(f"{uid}: RTC sync OK")
            else:
                print(f"{uid}: RTC sync failed/timeout")

        remote_files = request_remote_file_list(
            control_sock=control_sock,
            device_ip=device_ip,
            control_port=args.discover_port,
            timeout_s=args.file_list_timeout,
        )
        if not remote_files:
            base_result["status"] = "skip"
            base_result["message"] = f"No files reported by {display_id} ({device_ip})."
            return base_result

        seen = load_received_filenames(file_log_root, uid, device_short_uid=short_uid)
        if args.transfer_latest_even_if_seen:
            next_file = select_most_recent_file(
                remote_files,
                prefer_prefix=args.prefer_file_prefix,
                target_yymmdd=target_yymmdd,
                tr_only=args.tr_only,
            )
        else:
            next_file = select_most_recent_unsaved_file(
                remote_files,
                seen,
                prefer_prefix=args.prefer_file_prefix,
                target_yymmdd=target_yymmdd,
                tr_only=args.tr_only,
            )

        if not next_file:
            base_result["status"] = "skip"
            base_result["message"] = f"No new files to fetch for {display_id}."
            return base_result

        base_result["source_filename"] = next_file
        print(f"{display_id}: {len(remote_files)} remote file(s), {len(seen)} already saved, next={next_file}")
        extra_tag = ""
        if args.transfer_latest_even_if_seen:
            extra_tag = dt.datetime.now().strftime("R%Y%m%d_%H%M%S")
        local_name = build_local_filename(next_file, uid, extra_tag=extra_tag, device_short_uid=short_uid)
        out_dir = file_output_root / display_id
        local_name = ensure_unique_filename(local_name, out_dir)

        saved_path = transfer_file_protocol(
            control_sock=control_sock,
            device_ip=device_ip,
            control_port=args.discover_port,
            local_bind_ip=bind_ip,
            requested_filename=next_file,
            output_dir=out_dir,
            device_uid=uid,
            device_short_uid=short_uid,
            log_root=file_log_root,
            local_filename=local_name,
            timeout_s=max(args.download_timeout, 30.0),
            tolerant_integrity=args.transfer_tolerant,
            mark_partial_received=args.mark_partial_received,
        )
        base_result["saved_path"] = str(saved_path)
        base_result["status"] = "saved"
        base_result["message"] = f"Saved file for {display_id}: {saved_path}"
        return base_result
    except Exception as exc:
        source = str(base_result.get("source_filename", "")).strip()
        if source:
            base_result["message"] = f"Transfer failed for {display_id} file {source}: {exc}"
        else:
            base_result["message"] = f"Transfer failed for {display_id}: {exc}"
        base_result["error_text"] = str(exc)
        base_result["status"] = "error"
        return base_result
    finally:
        base_result["duration_s"] = time.monotonic() - started
        control_sock.close()


def run_discovery(args: argparse.Namespace) -> int:
    run_id = f"DISC_{int(time.time() * 1000)}"
    db_enabled = bool(getattr(args, "db_log", True))
    db_path = Path(str(getattr(args, "db_path", "data/bsm_network.db"))).expanduser()
    if db_enabled:
        try:
            init_db(db_path)
        except Exception as exc:
            print(f"Warning: failed to initialize DB '{db_path}': {exc}")
            db_enabled = False

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    requested_bind = (args.bind or "").strip() or "0.0.0.0"
    bound_bind = requested_bind
    try:
        sock.bind((requested_bind, args.port))
    except OSError as exc:
        if requested_bind != "0.0.0.0":
            try:
                sock.bind(("0.0.0.0", args.port))
                bound_bind = "0.0.0.0"
                print(
                    f"Warning: bind to {requested_bind}:{args.port} failed ({exc}). "
                    "Falling back to 0.0.0.0."
                )
            except OSError:
                sock.close()
                raise
        else:
            sock.close()
            raise
    sock.settimeout(0.2)

    poll_message = b"POLL_UID"
    if args.discover_ip:
        discover_ips = [args.discover_ip]
    else:
        auto_ip = detect_broadcast_ip(bound_bind)
        discover_ips = [auto_ip]
        if auto_ip != "255.255.255.255":
            discover_ips.append("255.255.255.255")
    discovered: dict[str, dict[str, str | int]] = {}
    start = time.monotonic()

    print(f"{args.host_label} discovery started.")
    print(
        f"Polling {', '.join(f'udp://{ip}:{args.discover_port}' for ip in discover_ips)} "
        f"from local udp://{bound_bind}:{args.port}"
    )
    print(
        f"Waiting up to {args.discover_timeout:.1f}s for replies @ "
        f"{dt.datetime.now().strftime('%H:%M:%S')}"
    )

    for _ in range(args.discover_attempts):
        for discover_ip in discover_ips:
            sock.sendto(poll_message, (discover_ip, args.discover_port))
        if args.discover_interval > 0:
            time.sleep(args.discover_interval)

    deadline = start + args.discover_timeout
    while time.monotonic() < deadline:
        try:
            data, (src_ip, src_port) = sock.recvfrom(args.buffer_size)
        except socket.timeout:
            continue

        payload = data.decode("utf-8", errors="replace").strip()
        parsed = parse_id_response(payload)
        if parsed is None:
            continue
        uid = str(parsed["unique_id"])
        discovered[uid] = {
            "unique_id": uid,
            "device_ip": parsed["device_ip"],
            "udp_target_ip": parsed["udp_target_ip"],
            "udp_target_port": parsed["udp_target_port"],
            "network_uid": parsed.get("network_uid", ""),
            "network_hostname": "",
            "wifi_mac": "",
            "recv_ip": src_ip,
            "recv_port": src_port,
            "last_seen": dt.datetime.now().isoformat(timespec="seconds"),
        }

    if not discovered:
        sock.close()
        print("No Arduino IDs discovered.")
        if db_enabled:
            log_discovery_event(db_path=db_path, run_id=run_id, status="none", message="No Arduino IDs discovered.")
        return 1

    rows = [discovered[uid] for uid in sorted(discovered)]
    for row in rows:
        device_ip = str(row["device_ip"] or row["recv_ip"])
        net = request_network_uid(
            control_sock=sock,
            device_ip=device_ip,
            control_port=args.discover_port,
            timeout_s=1.0,
        )
        if not net:
            continue
        row["network_uid"] = net.get("network_uid", "") or row.get("network_uid", "")
        row["network_hostname"] = net.get("network_hostname", "")
        row["wifi_mac"] = net.get("wifi_mac", "")

    device_to_ap, ap_limits, default_ap, device_to_burrow = _build_ap_routing_config(args)
    default_warned: set[str] = set()
    short_uid_to_uids: dict[str, list[str]] = {}

    for row in rows:
        uid = str(row.get("unique_id", ""))
        ap_id, source = _resolve_ap_for_device(row, device_to_ap, default_ap)
        burrow_id = _resolve_burrow_for_device(row, device_to_burrow)
        short_uid = _short_uid_from_unique_id(uid, 6)

        row["ap_id"] = ap_id
        row["ap_source"] = source
        row["burrow_id"] = burrow_id
        row["short_uid"] = short_uid
        row["short_uid_collision"] = 0
        row["short_uid_collision_note"] = ""

        if short_uid:
            short_uid_to_uids.setdefault(short_uid, []).append(uid)
            if db_enabled:
                existing = [x for x in find_unique_ids_by_short_uid(db_path, short_uid) if x and x != uid]
                if existing:
                    row["short_uid_collision"] = 1
                    row["short_uid_collision_note"] = "Existing: " + ",".join(sorted(set(existing)))
                    msg = f"Short UID collision for {uid}: {short_uid} already used by {','.join(sorted(set(existing)))}"
                    print(f"Warning: {msg}")
                    log_discovery_event(
                        db_path=db_path,
                        run_id=run_id,
                        status="short_uid_collision",
                        row=row,
                        message=msg,
                    )

        if source == "default":
            if uid not in default_warned:
                default_warned.add(uid)
                print(
                    f"Warning: {uid} has no AP mapping; assigned to default bucket "
                    f"'{default_ap}' (limit={ap_limits.get(default_ap, 1)})."
                )

    # Check collisions among devices discovered in this run.
    for short_uid, uid_list in short_uid_to_uids.items():
        uniq = sorted(set(uid_list))
        if len(uniq) <= 1:
            continue
        joined = ",".join(uniq)
        for row in rows:
            if str(row.get("short_uid", "")) != short_uid:
                continue
            row["short_uid_collision"] = 1
            prev = str(row.get("short_uid_collision_note", ""))
            run_note = f"Run: {joined}"
            row["short_uid_collision_note"] = (prev + "; " + run_note).strip("; ")
            msg = f"Short UID collision in current discovery run: {short_uid} -> {joined}"
            print(f"Warning: {msg}")
            if db_enabled:
                log_discovery_event(
                    db_path=db_path,
                    run_id=run_id,
                    status="short_uid_collision",
                    row=row,
                    message=msg,
                )

    for row in rows:
        if db_enabled:
            upsert_device(db_path, row)
            log_discovery_event(
                db_path=db_path,
                run_id=run_id,
                status="discovered",
                row=row,
                message="Device discovered and mapped.",
            )

    print(f"Discovered {len(rows)} Arduino device(s):")
    print("unique_id, short_uid, network_uid, udp_target_ip, device_ip, recv_ip")
    for row in rows:
        print(
            f"{row['unique_id']}, {row.get('short_uid', '')}, {row.get('network_uid', '')}, "
            f"{row['udp_target_ip']}, {row['device_ip']}, {row['recv_ip']}"
        )
    ap_counts = Counter(str(row.get("ap_id", default_ap)) for row in rows)
    print("AP assignment summary:")
    for ap_id in sorted(ap_counts):
        limit = ap_limits.get(ap_id, ap_limits.get("DEFAULT", 1))
        print(f"  {ap_id}: devices={ap_counts[ap_id]} limit={limit}")

    if args.discover_csv:
        csv_path = Path(args.discover_csv).expanduser()
        write_discovery_csv(csv_path, rows)
        print(f"Discovery table written: {csv_path}")

    print(f"Waiting {args.post_poll_wait:.1f}s before data retrieval...")
    if args.post_poll_wait > 0:
        time.sleep(args.post_poll_wait)

    if args.sync_time_only:
        if not args.sync_time:
            print("Sync-only mode requested but --no-sync-time is set; nothing to do.")
            sock.close()
            return 2

        ok_count = 0
        fail_count = 0
        for row in rows:
            uid = str(row["unique_id"])
            device_ip = str(row["device_ip"] or row["recv_ip"])
            sync_epoch = int(time.time() + (args.time_offset_hours * 3600.0))
            synced = send_time_sync(
                control_sock=sock,
                device_ip=device_ip,
                control_port=args.discover_port,
                epoch=sync_epoch,
                timeout_s=3.0,
            )
            if synced:
                ok_count += 1
                print(f"{uid}: RTC sync OK")
            else:
                fail_count += 1
                print(f"{uid}: RTC sync failed/timeout")

        print(f"RTC sync-only summary: ok={ok_count}, failed={fail_count}")
        sock.close()
        return 0 if fail_count == 0 else 1

    if args.transfer_latest_file:
        file_output_root = Path(args.file_output_dir).expanduser()
        file_log_root = Path(args.file_log_dir).expanduser()
        target_yymmdd = _resolve_target_yymmdd(args)
        day_mode = (getattr(args, "file_day", "yesterday") or "yesterday").lower()
        if target_yymmdd:
            print(
                f"Transfer selection mode: {day_mode} ({target_yymmdd}), "
                f"prefix={args.prefer_file_prefix}, tr_only={args.tr_only}"
            )
        else:
            print(
                f"Transfer selection mode: latest available, "
                f"prefix={args.prefer_file_prefix}, tr_only={args.tr_only}"
            )
        if args.max_concurrent_transfers > 0:
            global_limit = int(args.max_concurrent_transfers)
        else:
            global_limit = max(1, sum(max(1, v) for v in ap_limits.values()))
        print(f"Transfer concurrency: global={global_limit}, ap_limits={ap_limits}")

        slot_manager = APSlotManager(ap_limits=ap_limits, global_limit=global_limit)
        # Use one worker per device so waiting on AP slot limits does not
        # starve tasks assigned to other AP buckets.
        worker_count = max(1, len(rows))
        transfer_results: list[dict[str, str | float]] = []

        def _worker(row: dict[str, str | int]) -> dict[str, str | float]:
            ap_id = str(row.get("ap_id", default_ap))
            uid = str(row.get("unique_id", ""))
            slot_manager.acquire(ap_id)
            running_total, running_by_ap = slot_manager.snapshot()
            print(f"{uid}: acquired slot ap={ap_id} running_total={running_total} running_by_ap={running_by_ap}")
            if db_enabled:
                log_slot_event(
                    db_path=db_path,
                    run_id=run_id,
                    unique_id=uid,
                    ap_id=ap_id,
                    action="acquire",
                    running_total=running_total,
                    running_on_ap=int(running_by_ap.get(ap_id, 0)),
                )
            try:
                return _transfer_latest_file_for_device(
                    row=row,
                    args=args,
                    bind_ip=bound_bind,
                    target_yymmdd=target_yymmdd,
                    file_output_root=file_output_root,
                    file_log_root=file_log_root,
                )
            finally:
                slot_manager.release(ap_id)
                running_total, running_by_ap = slot_manager.snapshot()
                print(f"{uid}: released slot ap={ap_id} running_total={running_total} running_by_ap={running_by_ap}")
                if db_enabled:
                    log_slot_event(
                        db_path=db_path,
                        run_id=run_id,
                        unique_id=uid,
                        ap_id=ap_id,
                        action="release",
                        running_total=running_total,
                        running_on_ap=int(running_by_ap.get(ap_id, 0)),
                    )

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_worker, row) for row in rows]
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                except Exception as exc:
                    result = {
                        "ap_id": default_ap,
                        "status": "error",
                        "message": f"Transfer worker failed: {exc}",
                        "error_text": str(exc),
                        "duration_s": 0.0,
                    }
                transfer_results.append(result)
                print(str(result.get("message", "")))
                if db_enabled:
                    log_transfer_event(db_path=db_path, run_id=run_id, result=result)

        status_counts = Counter(str(r.get("status", "")) for r in transfer_results)
        ap_saved_counts = Counter(str(r.get("ap_id", default_ap)) for r in transfer_results if str(r.get("status", "")) == "saved")
        print(
            "Transfer summary: "
            f"saved={status_counts.get('saved', 0)} "
            f"skip={status_counts.get('skip', 0)} "
            f"error={status_counts.get('error', 0)}"
        )
        if ap_saved_counts:
            print("Saved-by-AP summary:")
            for ap_id in sorted(ap_saved_counts):
                print(f"  {ap_id}: {ap_saved_counts[ap_id]}")
    else:
        download_root = Path(args.download_dir).expanduser()
        for row in rows:
            uid = str(row["unique_id"])
            device_ip = str(row["device_ip"] or row["recv_ip"])
            if args.sync_time:
                sync_epoch = int(time.time() + (args.time_offset_hours * 3600.0))
                synced = send_time_sync(
                    control_sock=sock,
                    device_ip=device_ip,
                    control_port=args.discover_port,
                    epoch=sync_epoch,
                    timeout_s=3.0,
                )
                if synced:
                    print(f"{uid}: RTC sync OK")
                else:
                    print(f"{uid}: RTC sync failed/timeout")
            device_rows = collect_device_lines(sock, row, args)
            if not device_rows:
                print(f"No data received from {uid}.")
                continue

            timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            short_uid = str(row.get("short_uid", "")).strip().upper()
            suffix = short_uid if short_uid else (uid[-6:] if len(uid) >= 6 else uid)
            out_path = download_root / f"{suffix}_{timestamp}.csv"
            save_device_data_csv(out_path, device_rows)
            print(f"Saved {len(device_rows)} line(s) for {uid} -> {out_path}")

    sock.close()
    return 0
