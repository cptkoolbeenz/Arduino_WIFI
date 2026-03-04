#!/usr/bin/env python3
"""Listen for UDP packets from Arduino_WIFI.ino and parse payload CSV.

Expected payload format:
    device_id,unix_time,sample
Example:
    MOM01,1739936401,512
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import ipaddress
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UDP listener for Arduino_WIFI.ino payloads"
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Local interface/IP to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5005,
        help="UDP port to listen on (default: 5005)",
    )
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=2048,
        help="Max UDP datagram size in bytes (default: 2048)",
    )
    parser.add_argument(
        "--csv-log",
        default="",
        help="Optional CSV file path to append parsed packets",
    )
    parser.add_argument(
        "--ack",
        action="store_true",
        help="Reply to sender with a simple ACK message",
    )
    parser.add_argument(
        "--host-label",
        default="Mac",
        help="Name shown in startup output for the host machine (default: Mac)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Broadcast poll request and print discovered Arduino unique IDs",
    )
    parser.add_argument(
        "--discover-ip",
        default="",
        help="Broadcast IP for discovery polls (default: auto-detect subnet broadcast)",
    )
    parser.add_argument(
        "--discover-port",
        type=int,
        default=8888,
        help="UDP port Arduino listens on for discovery polls (default: 8888)",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for discovery replies (default: 3.0)",
    )
    parser.add_argument(
        "--discover-attempts",
        type=int,
        default=3,
        help="How many poll broadcasts to send (default: 3)",
    )
    parser.add_argument(
        "--discover-interval",
        type=float,
        default=0.5,
        help="Seconds between poll broadcasts (default: 0.5)",
    )
    parser.add_argument(
        "--discover-csv",
        default="",
        help="Optional CSV path to write discovered device table",
    )
    parser.add_argument(
        "--download-command",
        default="DOWNLOAD_DATA",
        help="UDP command sent to each Arduino to trigger data burst (default: DOWNLOAD_DATA)",
    )
    parser.add_argument(
        "--download-lines",
        type=int,
        default=4,
        help="How many CSV payload lines to capture per Arduino (default: 4)",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait per Arduino when capturing payload lines (default: 8.0)",
    )
    parser.add_argument(
        "--post-poll-wait",
        type=float,
        default=10.0,
        help="Seconds to wait after polling before starting downloads (default: 10.0)",
    )
    parser.add_argument(
        "--download-dir",
        default="data",
        help="Directory for per-device downloaded CSV files (default: data)",
    )
    return parser.parse_args()


def parse_payload(payload: str) -> dict[str, str | int] | None:
    parts = [p.strip() for p in payload.split(",")]
    if len(parts) != 3:
        return None

    device_id, unix_time_raw, sample_raw = parts

    try:
        unix_time = int(unix_time_raw)
        sample = int(sample_raw)
    except ValueError:
        return None

    return {
        "device_id": device_id,
        "unix_time": unix_time,
        "sample": sample,
    }


def append_csv(path: Path, row: dict[str, str | int], src_ip: str, src_port: int) -> None:
    exists = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["received_at", "src_ip", "src_port", "device_id", "unix_time", "sample"])
        writer.writerow(
            [
                dt.datetime.now().isoformat(timespec="seconds"),
                src_ip,
                src_port,
                row["device_id"],
                row["unix_time"],
                row["sample"],
            ]
        )


def detect_lan_ip() -> str:
    """Best-effort local LAN IP detection for startup guidance."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect does not send traffic; it selects the outbound interface.
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def detect_broadcast_ip(bind_ip: str) -> str:
    """Best-effort subnet broadcast detection on macOS via ifconfig."""
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

    # Backward compatible:
    # - "ID,<uid>"
    # New format:
    # - "ID,<uid>,<device_ip>,<udp_target_ip>,<udp_target_port>"
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


def write_discovery_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "unique_id",
                "device_ip",
                "udp_target_ip",
                "udp_target_port",
                "recv_ip",
                "recv_port",
                "last_seen",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def save_device_data_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "saved_at",
                "unique_id",
                "device_ip",
                "src_ip",
                "src_port",
                "unix_time",
                "sample",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def collect_device_lines(
    sock: socket.socket,
    device: dict[str, str | int],
    args: argparse.Namespace,
) -> list[dict[str, str | int]]:
    uid = str(device["unique_id"])
    device_ip = str(device["device_ip"] or device["recv_ip"])

    # Command each Arduino directly on its command/listen UDP port.
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
    print(f"Waiting up to {args.discover_timeout:.1f}s for replies.")

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
        print(
            f"{row['unique_id']}, {row['udp_target_ip']}, "
            f"{row['device_ip']}, {row['recv_ip']}"
        )

    if args.discover_csv:
        csv_path = Path(args.discover_csv).expanduser()
        write_discovery_csv(csv_path, rows)
        print(f"Discovery table written: {csv_path}")

    print(f"Waiting {args.post_poll_wait:.1f}s before data retrieval...")
    if args.post_poll_wait > 0:
        time.sleep(args.post_poll_wait)

    download_root = Path(args.download_dir).expanduser()
    for row in rows:
        uid = str(row["unique_id"])
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


def main() -> int:
    args = parse_args()

    csv_path = Path(args.csv_log).expanduser() if args.csv_log else None

    if args.discover:
        return run_discovery(args)

    suggested_ip = detect_lan_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))

    print(f"{args.host_label} UDP listener ready.")
    print(f"Listening on udp://{args.bind}:{args.port}")
    print(f"Set Arduino UDP_TARGET_IP to: {suggested_ip}")
    print(f"Set Arduino UDP_TARGET_PORT to: {args.port}")
    if csv_path:
        print(f"CSV logging enabled: {csv_path}")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            data, (src_ip, src_port) = sock.recvfrom(args.buffer_size)
            payload = data.decode("utf-8", errors="replace").strip()
            parsed = parse_payload(payload)

            now = dt.datetime.now().isoformat(timespec="seconds")
            if parsed is None:
                print(f"[{now}] {src_ip}:{src_port} RAW: {payload}")
            else:
                print(
                    f"[{now}] {src_ip}:{src_port} "
                    f"device={parsed['device_id']} unix_time={parsed['unix_time']} sample={parsed['sample']}"
                )
                if csv_path:
                    append_csv(csv_path, parsed, src_ip, src_port)

            if args.ack:
                ack = f"ACK,{now}".encode("utf-8")
                sock.sendto(ack, (src_ip, src_port))

    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
