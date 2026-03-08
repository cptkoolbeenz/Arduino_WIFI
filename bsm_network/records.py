from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path


def append_csv(path: Path, row: dict[str, str | int], src_ip: str, src_port: int) -> None:
    exists = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["received_at", "src_ip", "src_port", "device_id", "unix_time", "sample"])
        writer.writerow([
            dt.datetime.now().isoformat(timespec="seconds"),
            src_ip,
            src_port,
            row["device_id"],
            row["unix_time"],
            row["sample"],
        ])


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


def append_file_receive_log(
    log_root: Path,
    device_uid: str,
    source_filename: str,
    saved_path: Path,
    transfer_id: str,
    total_bytes: int,
    checksum_hex: str,
    transfer_seconds: float,
) -> Path:
    log_root.mkdir(parents=True, exist_ok=True)
    device_uid_6 = (device_uid[-6:] if len(device_uid) >= 6 else device_uid).upper()
    log_path = log_root / f"{device_uid_6}.csv"
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
                    "transfer_seconds",
                ]
            )
        writer.writerow(
            [
                dt.datetime.now().isoformat(timespec="seconds"),
                device_uid_6,
                source_filename,
                str(saved_path),
                transfer_id,
                total_bytes,
                checksum_hex,
                f"{transfer_seconds:.3f}",
            ]
        )

    return log_path


def load_received_filenames(log_root: Path, device_uid: str) -> set[str]:
    device_uid_6 = (device_uid[-6:] if len(device_uid) >= 6 else device_uid).upper()
    log_path = log_root / f"{device_uid_6}.csv"
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


def build_local_filename(remote_filename: str, device_uid: str, extra_tag: str = "") -> str:
    base = Path(remote_filename).name
    stem = Path(base).stem
    suffix = Path(base).suffix or ".TXT"

    uid6 = (device_uid[-6:] if len(device_uid) >= 6 else device_uid).upper()
    tag = ""
    if extra_tag:
        safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", extra_tag)
        tag = f"_{safe_tag}"
    if len(stem) == 8 and stem.isdigit():
        return f"DL_{stem}_{uid6}{tag}{suffix.upper()}"

    safe_stem = stem.replace(" ", "_")
    return f"DL_{safe_stem}_{uid6}{tag}{suffix.upper()}"


def ensure_unique_filename(base_name: str, output_dir: Path) -> str:
    candidate = output_dir / base_name
    if not candidate.exists():
        return base_name

    stem = candidate.stem
    suffix = candidate.suffix
    idx = 2
    while True:
        alt = f"{stem}_{idx}{suffix}"
        if not (output_dir / alt).exists():
            return alt
        idx += 1


def select_most_recent_unsaved_file(remote_filenames: list[str], already_received: set[str]) -> str | None:
    dated: list[tuple[str, str]] = []
    undated: list[str] = []
    for name in remote_filenames:
        m = re.search(r"DL(\\d{6})", name.upper())
        if m:
            dated.append((m.group(1), name))
        else:
            undated.append(name)

    # Prefer files with parseable YYMMDD token, newest date first.
    for _, name in sorted(dated, key=lambda x: x[0], reverse=True):
        if name not in already_received:
            return name

    # Fallback for any legacy/unexpected naming.
    for name in sorted(undated, reverse=True):
        if name not in already_received:
            return name
    return None


def select_most_recent_file(remote_filenames: list[str]) -> str | None:
    if not remote_filenames:
        return None

    dated: list[tuple[str, str]] = []
    undated: list[str] = []
    for name in remote_filenames:
        m = re.search(r"DL(\d{6})", name.upper())
        if m:
            dated.append((m.group(1), name))
        else:
            undated.append(name)

    if dated:
        return sorted(dated, key=lambda x: x[0], reverse=True)[0][1]
    return sorted(undated, reverse=True)[0]
