from __future__ import annotations

import socket
import time
import zlib
from pathlib import Path

from .records import append_file_receive_log


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
    return {"device_id": device_id, "unix_time": unix_time, "sample": sample}


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


def _new_transfer_id(prefix: str = "T") -> str:
    return f"{prefix}{int(time.time() * 1000)}"


def _partial_path(path: Path, transfer_id: str) -> Path:
    stem = path.stem
    suffix = path.suffix
    return path.with_name(f"{stem}_PARTIAL_{transfer_id}{suffix}")


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


def send_time_sync(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    epoch: int | None = None,
    timeout_s: float = 3.0,
) -> bool:
    if epoch is None:
        epoch = int(time.time())

    msg = f"SET_TIME,{epoch}".encode("utf-8")
    control_sock.sendto(msg, (device_ip, control_port))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, (src_ip, _) = control_sock.recvfrom(2048)
        except socket.timeout:
            continue
        if src_ip != device_ip:
            continue
        line = data.decode("utf-8", errors="replace").strip()
        if line.startswith("ACK_TIME,"):
            return True
        if line.startswith("ERR_TIME,"):
            return False
    return False


def transfer_file_protocol(
    control_sock: socket.socket,
    device_ip: str,
    control_port: int,
    local_bind_ip: str,
    requested_filename: str,
    output_dir: Path,
    device_uid: str | None = None,
    log_root: Path | None = None,
    local_filename: str | None = None,
    timeout_s: float = 30.0,
    tolerant_integrity: bool = False,
    mark_partial_received: bool = False,
) -> Path:
    transfer_id = _new_transfer_id("T")
    transfer_start = time.monotonic()
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

    final_name = local_filename if local_filename else filename
    out_path = output_dir / final_name
    stream_crc32 = 0
    expected_offset = 0
    bytes_written = 0
    next_progress_report = 10
    integrity_status = "verified"

    with out_path.open("wb") as out:
        while True:
            try:
                header = _recv_until_newline(conn).decode("utf-8", errors="replace").strip()
            except (ConnectionError, TimeoutError, ValueError, socket.timeout) as exc:
                if tolerant_integrity and bytes_written > 0:
                    integrity_status = "partial_no_eof"
                    print(f"[{device_ip}] WARNING: stream ended before valid EOF ({exc}); saving partial file.")
                    break
                raise
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
                    if tolerant_integrity:
                        integrity_status = "partial_eof_mismatch"
                        print(
                            f"[{device_ip}] WARNING: EOF mismatch (remote bytes={total_bytes}, "
                            f"local bytes={bytes_written}, remote crc={file_crc}, local crc={local_crc}); "
                            "saving partial file."
                        )
                        break
                    raise ValueError("EOF integrity check failed (size/crc mismatch)")
                print(f"[{device_ip}] EOF verified: bytes={total_bytes} crc32={local_crc}")
                break

    conn.close()
    if integrity_status != "verified":
        partial_out_path = _partial_path(out_path, transfer_id)
        out_path.rename(partial_out_path)
        out_path = partial_out_path

    done_msg = f"DONE,{transfer_id}".encode("utf-8")
    control_sock.sendto(done_msg, (device_ip, control_port))
    print(f"[{device_ip}] DONE sent. Saved ({integrity_status}) -> {out_path}")
    transfer_seconds = time.monotonic() - transfer_start
    print(f"[{device_ip}] Transfer time: {transfer_seconds:.2f}s")

    should_mark_received = integrity_status == "verified" or (
        integrity_status != "verified" and mark_partial_received
    )
    if device_uid and log_root and should_mark_received:
        append_file_receive_log(
            log_root=log_root,
            device_uid=device_uid,
            source_filename=filename,
            saved_path=out_path,
            transfer_id=transfer_id,
            total_bytes=bytes_written,
            checksum_hex=f"{(stream_crc32 & 0xFFFFFFFF):08x}",
            transfer_seconds=transfer_seconds,
        )

    return out_path
