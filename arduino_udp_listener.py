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
import socket
import sys
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


def main() -> int:
    args = parse_args()

    csv_path = Path(args.csv_log).expanduser() if args.csv_log else None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind, args.port))

    print(f"Listening on udp://{args.bind}:{args.port}")
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
