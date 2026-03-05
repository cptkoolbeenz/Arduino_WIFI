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
import zlib
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
    parser.add_argument(
        "--transfer-latest-file",
        action="store_true",
        help="After discovery, list remote files and transfer the latest unsaved file",
    )
    parser.add_argument(
        "--file-list-timeout",
        type=float,
        default=8.0,
        help="Seconds to wait for LIST_FILES response per device (default: 8.0)",
    )
    parser.add_argument(
        "--file-output-dir",
        default="data/files",
        help="Directory for transferred files (default: data/files)",
    )
    parser.add_argument(
        "--file-log-dir",
        default="data/file_logs",
        help="Directory for per-device file transfer logs (default: data/file_logs)",
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


def _recv_until_newline(sock: socket.socket, limit: int = 512) -> bytes:
    data = bytearray()
    while len(data) < limit:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("Socket closed while waiting for newline-terminated header")
        data.extend(b)
        if b == b"\n":
            return bytes(data)
    raise ValueError("Header exceeded maximum length")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        part = sock.recv(n - len(chunks))
        if not part:
            raise ConnectionError("Socket closed during payload read")
        chunks.extend(part)
    return bytes(chunks)


def append_file_receive_log(
    log_root: Path,
    device_uid: str,
    source_filename: str,
    saved_path: Path,
    transfer_id: str,
    total_bytes: int,
    checksum_hex: str,
) -> Path:
    """Append one successful file receive event for a specific device."""
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"{device_uid}.csv"
    exists = log_path.exists()

    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(
                [
                    "received_at",
                    "device_uid",
                    "source_filename",
                    "saved_path",
                    "transfer_id",
                    "total_bytes",
                    "checksum_crc32",
                ]
            )
        writer.writerow(
            [
                dt.datetime.now().isoformat(timespec="seconds"),
                device_uid,
                source_filename,
                str(saved_path),
                transfer_id,
                total_bytes,
                checksum_hex,
            ]
        )

    return log_path


def load_received_filenames(log_root: Path, device_uid: str) -> set[str]:
    """Return filenames already received for this device from its log CSV."""
    log_path = log_root / f"{device_uid}.csv"
    if not log_path.exists():
        return set()

    seen: set[str] = set()
    with log_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("source_filename") or "").strip()
            if name:
                seen.add(name)
    return seen


def select_most_recent_unsaved_file(
    remote_filenames: list[str],
    already_received: set[str],
) -> str | None:
    """Pick latest filename not yet saved (assumes sortable timestamp-style names)."""
    for name in sorted(remote_filenames, reverse=True):
        if name not in already_received:
            return name
    return None


def _new_transfer_id(prefix: str = "T") -> str:
    return f"{prefix}{int(time.time() * 1000)}"


def request_remote_file_list(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    timeout_s: float,
) -> list[str]:
    transfer_id = _new_transfer_id("L")
    cmd = f"LIST_FILES,{transfer_id}".encode("utf-8")
    control_sock.sendto(cmd, (device_ip, control_port))

    files: list[str] = []
    seen: set[str] = set()
    got_end = False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
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
            raise RuntimeError(line)
        if msg_type == "FILE_LIST_BEGIN":
            continue
        if msg_type == "FILE_ITEM" and len(parts) >= 3:
            name = parts[2]
            if name and name not in seen:
                seen.add(name)
                files.append(name)
            continue
        if msg_type == "FILE_LIST_END":
            got_end = True
            break

    if not got_end:
        raise TimeoutError(f"LIST_FILES timeout for {device_ip}")
    return files


def transfer_file_protocol(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    local_bind_ip: str,
    requested_filename: str,
    output_dir: Path,
    device_uid: str | None = None,
    log_root: Path | None = None,
    timeout_s: float = 30.0,
) -> Path:
    """Run START_FILE -> FILE_INFO -> TCP chunk stream -> DONE protocol.

    Expected UDP control messages:
      START_FILE,<transfer_id>,<filename>,<tcp_port>
      FILE_INFO,<transfer_id>,<filename>,<size_bytes>,<chunk_size>
      ACK_CHUNK,<transfer_id>,<chunk_index>
      RESUME,<transfer_id>,<offset_bytes>
      DONE,<transfer_id>

    Expected TCP frames from Arduino (line header + payload):
      CHUNK,<transfer_id>,<chunk_index>,<offset>,<payload_len>,<crc32_hex>\\n
      <payload bytes>
      EOF,<transfer_id>,<total_bytes>,<full_crc32_hex>\\n
    """
    transfer_id = _new_transfer_id("T")
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{device_ip}] Transfer start: id={transfer_id} file={requested_filename}")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((local_bind_ip, 0))
    server.listen(1)
    server.settimeout(timeout_s)
    tcp_port = server.getsockname()[1]

    start_msg = f"START_FILE,{transfer_id},{requested_filename},{tcp_port}".encode("utf-8")
    control_sock.sendto(start_msg, (device_ip, control_port))
    print(f"[{device_ip}] START_FILE sent (tcp_port={tcp_port})")

    # Wait for FILE_INFO over UDP control channel.
    file_size = None
    chunk_size = None
    filename = requested_filename
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue

        line = data.decode("utf-8", errors="replace").strip()
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5 or parts[0] != "FILE_INFO" or parts[1] != transfer_id:
            continue
        filename = parts[2]
        file_size = int(parts[3])
        chunk_size = int(parts[4])
        print(f"[{device_ip}] FILE_INFO: name={filename} size={file_size} chunk={chunk_size}")
        break

    if file_size is None or chunk_size is None:
        server.close()
        raise TimeoutError("Did not receive FILE_INFO for transfer")

    conn, addr = server.accept()
    conn.settimeout(timeout_s)
    server.close()
    print(f"[{device_ip}] TCP connected from {addr[0]}:{addr[1]}")

    out_path = output_dir / filename
    stream_crc32 = 0
    expected_offset = 0
    bytes_written = 0
    next_progress_report = 10

    with out_path.open("wb") as out:
        while True:
            header = _recv_until_newline(conn).decode("utf-8", errors="replace").strip()
            parts = [p.strip() for p in header.split(",")]
            if not parts:
                continue

            if parts[0] == "CHUNK":
                if len(parts) != 6 or parts[1] != transfer_id:
                    continue
                chunk_index = int(parts[2])
                offset = int(parts[3])
                payload_len = int(parts[4])
                crc_hex = parts[5].lower()

                payload = _recv_exact(conn, payload_len)
                crc_actual = f"{(zlib.crc32(payload) & 0xFFFFFFFF):08x}"
                if offset != expected_offset or crc_actual != crc_hex:
                    resume_msg = f"RESUME,{transfer_id},{expected_offset}".encode("utf-8")
                    control_sock.sendto(resume_msg, (device_ip, control_port))
                    continue

                out.write(payload)
                stream_crc32 = zlib.crc32(payload, stream_crc32)
                expected_offset += payload_len
                bytes_written += payload_len
                if file_size > 0:
                    pct = int((bytes_written * 100) / file_size)
                    if pct >= next_progress_report:
                        print(f"[{device_ip}] Receiving {filename}: {pct}% ({bytes_written}/{file_size})")
                        next_progress_report += 10
                ack_msg = f"ACK_CHUNK,{transfer_id},{chunk_index}".encode("utf-8")
                control_sock.sendto(ack_msg, (device_ip, control_port))
                continue

            if parts[0] == "EOF":
                if len(parts) != 4 or parts[1] != transfer_id:
                    continue
                total_bytes = int(parts[2])
                file_crc = parts[3].lower()
                local_crc = f"{(stream_crc32 & 0xFFFFFFFF):08x}"
                if total_bytes != bytes_written or file_crc != local_crc:
                    raise ValueError("EOF integrity check failed (size/crc mismatch)")
                print(f"[{device_ip}] EOF verified: bytes={total_bytes} crc32={local_crc}")
                break

    conn.close()
    done_msg = f"DONE,{transfer_id}".encode("utf-8")
    control_sock.sendto(done_msg, (device_ip, control_port))
    print(f"[{device_ip}] DONE sent. Saved -> {out_path}")

    if device_uid and log_root:
        append_file_receive_log(
            log_root=log_root,
            device_uid=device_uid,
            source_filename=filename,
            saved_path=out_path,
            transfer_id=transfer_id,
            total_bytes=bytes_written,
            checksum_hex=f"{(stream_crc32 & 0xFFFFFFFF):08x}",
        )

    return out_path


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

    if args.transfer_latest_file:
        file_output_root = Path(args.file_output_dir).expanduser()
        file_log_root = Path(args.file_log_dir).expanduser()
        for row in rows:
            uid = str(row["unique_id"])
            device_ip = str(row["device_ip"] or row["recv_ip"])
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
                print(f"No files reported by {uid} ({device_ip}).")
                continue

            seen = load_received_filenames(file_log_root, uid)
            next_file = select_most_recent_unsaved_file(remote_files, seen)
            if not next_file:
                print(f"No new files to fetch for {uid}.")
                continue
            print(f"{uid}: {len(remote_files)} remote file(s), {len(seen)} already saved, next={next_file}")

            try:
                saved_path = transfer_file_protocol(
                    control_sock=sock,
                    device_ip=device_ip,
                    control_port=args.discover_port,
                    local_bind_ip=args.bind,
                    requested_filename=next_file,
                    output_dir=file_output_root / uid,
                    device_uid=uid,
                    log_root=file_log_root,
                    timeout_s=max(args.download_timeout, 30.0),
                )
                print(f"Saved file for {uid}: {saved_path}")
            except Exception as exc:
                print(f"Transfer failed for {uid} file {next_file}: {exc}")
    else:
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
