from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import re
import socket
import subprocess
import time
from pathlib import Path

from .protocol import parse_payload, request_remote_file_list, send_time_sync, transfer_file_protocol
from .records import (
    build_local_filename,
    load_received_filenames,
    save_device_data_csv,
    select_most_recent_unsaved_file,
    write_discovery_csv,
)


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
    }


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


def run_discovery(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(0.2)

    poll_message = b"POLL_UID"
    if args.discover_ip:
        discover_ips = [args.discover_ip]
    else:
        auto_ip = detect_broadcast_ip(args.bind)
        discover_ips = [auto_ip]
        if auto_ip != "255.255.255.255":
            discover_ips.append("255.255.255.255")
    discovered: dict[str, dict[str, str | int]] = {}
    start = time.monotonic()

    print(f"{args.host_label} discovery started.")
    print(
        f"Polling {', '.join(f'udp://{ip}:{args.discover_port}' for ip in discover_ips)} "
        f"from local udp://{args.bind}:{args.port}"
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
            "recv_ip": src_ip,
            "recv_port": src_port,
            "last_seen": dt.datetime.now().isoformat(timespec="seconds"),
        }

    if not discovered:
        sock.close()
        print("No Arduino IDs discovered.")
        return 1

    rows = [discovered[uid] for uid in sorted(discovered)]
    print(f"Discovered {len(rows)} Arduino device(s):")
    print("unique_id, udp_target_ip, device_ip, recv_ip")
    for row in rows:
        print(f"{row['unique_id']}, {row['udp_target_ip']}, {row['device_ip']}, {row['recv_ip']}")

    if args.discover_csv:
        csv_path = Path(args.discover_csv).expanduser()
        write_discovery_csv(csv_path, rows)
        print(f"Discovery table written: {csv_path}")

    print(f"Waiting {args.post_poll_wait:.1f}s before data retrieval...")
    if args.post_poll_wait > 0:
        time.sleep(args.post_poll_wait)

    if args.transfer_latest_file:
        file_output_root = Path(args.file_output_dir).expanduser()
        file_log_root = Path(args.file_log_dir).expanduser()
        for row in rows:
            uid = str(row["unique_id"])
            uid6 = (uid[-6:] if len(uid) >= 6 else uid).upper()
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
            try:
                remote_files = request_remote_file_list(
                    control_sock=sock,
                    device_ip=device_ip,
                    control_port=args.discover_port,
                    timeout_s=args.file_list_timeout,
                )
            except Exception as exc:
                print(f"File list failed for {uid} ({device_ip}): {exc}")
                continue

            if not remote_files:
                print(f"No files reported by {uid6} ({device_ip}).")
                continue

            seen = load_received_filenames(file_log_root, uid)
            next_file = select_most_recent_unsaved_file(remote_files, seen)
            if not next_file:
                print(f"No new files to fetch for {uid6}.")
                continue
            print(f"{uid6}: {len(remote_files)} remote file(s), {len(seen)} already saved, next={next_file}")

            try:
                local_name = build_local_filename(next_file, uid)
                saved_path = transfer_file_protocol(
                    control_sock=sock,
                    device_ip=device_ip,
                    control_port=args.discover_port,
                    local_bind_ip=args.bind,
                    requested_filename=next_file,
                    output_dir=file_output_root / uid6,
                    device_uid=uid,
                    log_root=file_log_root,
                    local_filename=local_name,
                    timeout_s=max(args.download_timeout, 30.0),
                    tolerant_integrity=args.transfer_tolerant,
                    mark_partial_received=args.mark_partial_received,
                )
                print(f"Saved file for {uid6}: {saved_path}")
            except Exception as exc:
                print(f"Transfer failed for {uid6} file {next_file}: {exc}")
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
            suffix = uid[-6:] if len(uid) >= 6 else uid
            out_path = download_root / f"{suffix}_{timestamp}.csv"
            save_device_data_csv(out_path, device_rows)
            print(f"Saved {len(device_rows)} line(s) for {uid} -> {out_path}")

    sock.close()
    return 0
