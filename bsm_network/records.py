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
                "short_uid",
                "network_uid",
                "firmware_version",
                "network_hostname",
                "wifi_mac",
                "device_ip",
                "udp_target_ip",
                "udp_target_port",
                "recv_ip",
                "recv_port",
                "ap_id",
                "ap_source",
                "burrow_id",
                "short_uid_collision",
                "short_uid_collision_note",
                "ready_filename",
                "ready_size",
                "ready_unix_ts",
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
    device_short_uid: str | None,
    source_filename: str,
    saved_path: Path,
    transfer_id: str,
    total_bytes: int,
    checksum_hex: str,
    transfer_seconds: float,
) -> Path:
    log_root.mkdir(parents=True, exist_ok=True)
    token = (device_short_uid or "").strip().upper()
    if not token:
        token = (device_uid[-6:] if len(device_uid) >= 6 else device_uid).upper()
    log_path = log_root / f"{token}.csv"
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
                token,
                source_filename,
                str(saved_path),
                transfer_id,
                total_bytes,
                checksum_hex,
                f"{transfer_seconds:.3f}",
            ]
        )

    return log_path


def load_received_filenames(log_root: Path, device_uid: str, device_short_uid: str | None = None) -> set[str]:
    token = (device_short_uid or "").strip().upper()
    if not token:
        token = (device_uid[-6:] if len(device_uid) >= 6 else device_uid).upper()
    log_path = log_root / f"{token}.csv"
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


def build_local_filename(
    remote_filename: str,
    device_uid: str,
    extra_tag: str = "",
    device_short_uid: str | None = None,
) -> str:
    base = Path(remote_filename).name
    stem = Path(base).stem
    suffix = Path(base).suffix or ".TXT"

    uid6 = (device_short_uid or "").strip().upper()
    if not uid6:
        uid6 = (device_uid[-6:] if len(device_uid) >= 6 else device_uid).upper()
    tag = ""
    if extra_tag:
        safe_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", extra_tag)
        tag = f"_{safe_tag}"
    file_prefix = "TR" if stem.upper().startswith("TR") else "DL"
    if len(stem) == 8 and stem.isdigit():
        return f"{file_prefix}_{stem}_{uid6}{tag}{suffix.upper()}"

    safe_stem = stem.replace(" ", "_")
    return f"{file_prefix}_{safe_stem}_{uid6}{tag}{suffix.upper()}"


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


def _normalize_prefer_prefix(prefer_prefix: str) -> str:
    p = (prefer_prefix or "TR").upper()
    return p if p in {"TR", "DL", "ANY"} else "TR"


def _dated_groups(remote_filenames: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str], list[tuple[str, str, str]]]:
    tr_dated: list[tuple[str, str]] = []
    dl_dated: list[tuple[str, str]] = []
    undated: list[str] = []
    all_dated: list[tuple[str, str, str]] = []
    for name in remote_filenames:
        upper = name.upper()
        m = re.search(r"(TR|DL)(\d{6})", upper)
        if m:
            yymmdd = m.group(2)
            prefix = m.group(1)
            all_dated.append((yymmdd, prefix, name))
            if prefix == "TR":
                tr_dated.append((yymmdd, name))
            else:
                dl_dated.append((yymmdd, name))
        else:
            undated.append(name)
    return tr_dated, dl_dated, undated, all_dated


def select_most_recent_unsaved_file(
    remote_filenames: list[str],
    already_received: set[str],
    prefer_prefix: str = "TR",
    target_yymmdd: str | None = None,
    tr_only: bool = False,
) -> str | None:
    prefer = _normalize_prefer_prefix(prefer_prefix)
    tr_dated, dl_dated, undated, all_dated = _dated_groups(remote_filenames)

    if target_yymmdd:
        target = target_yymmdd.strip()
        tr_target = [name for yymmdd, name in tr_dated if yymmdd == target]
        dl_target = [name for yymmdd, name in dl_dated if yymmdd == target]
        any_target = [name for yymmdd, _, name in all_dated if yymmdd == target]
        if prefer == "ANY":
            for name in sorted(any_target, reverse=True):
                if name not in already_received:
                    return name
            return None
        if prefer == "DL":
            for name in sorted(dl_target, reverse=True):
                if name not in already_received:
                    return name
            for name in sorted(tr_target, reverse=True):
                if name not in already_received:
                    return name
            return None
        for name in sorted(tr_target, reverse=True):
            if name not in already_received:
                return name
        if tr_only:
            return None
        for name in sorted(dl_target, reverse=True):
            if name not in already_received:
                return name
        return None

    if prefer == "ANY":
        for _, _, name in sorted(all_dated, key=lambda x: x[0], reverse=True):
            if name not in already_received:
                return name
    elif prefer == "DL":
        for _, name in sorted(dl_dated, key=lambda x: x[0], reverse=True):
            if name not in already_received:
                return name
        for _, name in sorted(tr_dated, key=lambda x: x[0], reverse=True):
            if name not in already_received:
                return name
    else:
        # Default: TR first, then DL.
        for _, name in sorted(tr_dated, key=lambda x: x[0], reverse=True):
            if name not in already_received:
                return name
        if tr_only:
            return None
        for _, name in sorted(dl_dated, key=lambda x: x[0], reverse=True):
            if name not in already_received:
                return name

    for name in sorted(undated, reverse=True):
        if name not in already_received:
            return name
    return None


def select_most_recent_file(
    remote_filenames: list[str],
    prefer_prefix: str = "TR",
    target_yymmdd: str | None = None,
    tr_only: bool = False,
) -> str | None:
    if not remote_filenames:
        return None

    prefer = _normalize_prefer_prefix(prefer_prefix)
    tr_dated, dl_dated, undated, all_dated = _dated_groups(remote_filenames)

    if target_yymmdd:
        target = target_yymmdd.strip()
        tr_target = [name for yymmdd, name in tr_dated if yymmdd == target]
        dl_target = [name for yymmdd, name in dl_dated if yymmdd == target]
        any_target = [name for yymmdd, _, name in all_dated if yymmdd == target]
        if prefer == "ANY":
            return sorted(any_target, reverse=True)[0] if any_target else None
        if prefer == "DL":
            if dl_target:
                return sorted(dl_target, reverse=True)[0]
            if tr_target:
                return sorted(tr_target, reverse=True)[0]
            return None
        if tr_target:
            return sorted(tr_target, reverse=True)[0]
        if tr_only:
            return None
        if dl_target:
            return sorted(dl_target, reverse=True)[0]
        return None

    if prefer == "ANY":
        if all_dated:
            return sorted(all_dated, key=lambda x: x[0], reverse=True)[0][2]
    elif prefer == "DL":
        if dl_dated:
            return sorted(dl_dated, key=lambda x: x[0], reverse=True)[0][1]
        if tr_dated:
            return sorted(tr_dated, key=lambda x: x[0], reverse=True)[0][1]
    else:
        if tr_dated:
            return sorted(tr_dated, key=lambda x: x[0], reverse=True)[0][1]
        if tr_only:
            return None
        if dl_dated:
            return sorted(dl_dated, key=lambda x: x[0], reverse=True)[0][1]

    return sorted(undated, reverse=True)[0]
