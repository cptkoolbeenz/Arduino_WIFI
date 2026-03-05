from __future__ import annotations

import argparse
import csv
import datetime as dt
import shutil
import subprocess
from pathlib import Path


def _load_sent_set(sent_log: Path) -> set[str]:
    if not sent_log.exists():
        return set()
    sent: set[str] = set()
    with sent_log.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rel = (row.get("relative_path") or "").strip()
            if rel:
                sent.add(rel)
    return sent


def _append_sent(sent_log: Path, relative_path: str, size_bytes: int) -> None:
    sent_log.parent.mkdir(parents=True, exist_ok=True)
    exists = sent_log.exists()
    with sent_log.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["sent_at", "relative_path", "size_bytes"])
        writer.writerow([
            dt.datetime.now().isoformat(timespec="seconds"),
            relative_path,
            size_bytes,
        ])


def _iter_source_files(source_root: Path) -> list[Path]:
    if not source_root.exists():
        return []
    return sorted([p for p in source_root.rglob("*") if p.is_file()])


def _upload_via_rclone(local_path: Path, remote_target: str) -> None:
    cmd = ["rclone", "copyto", str(local_path), remote_target]
    subprocess.run(cmd, check=True)


def _upload_to_local_dir(local_path: Path, source_root: Path, dest_root: Path) -> None:
    rel = local_path.relative_to(source_root)
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, dest)


def run_cloud_upload_cycle(args: argparse.Namespace) -> int:
    if not args.cloud_enabled:
        return 0

    source_root = Path(args.cloud_source_dir).expanduser()
    sent_log = Path(args.cloud_sent_log).expanduser()
    files = _iter_source_files(source_root)
    if not files:
        print("Cloud cycle: no local files found.")
        return 0

    sent = _load_sent_set(sent_log)
    pending: list[Path] = []
    for p in files:
        rel = p.relative_to(source_root).as_posix()
        if rel not in sent:
            pending.append(p)

    if not pending:
        print("Cloud cycle: no unsent files.")
        return 0

    use_local_dir = bool(args.cloud_local_dir)
    use_rclone = bool(args.cloud_rclone_remote)
    if not use_local_dir and not use_rclone:
        print(
            "Cloud cycle: pending files found but no cloud target is set; "
            "set --cloud-local-dir or --cloud-rclone-remote. "
            "skipping upload."
        )
        return 1

    dest_root = Path(args.cloud_local_dir).expanduser() if use_local_dir else None
    if dest_root:
        dest_root.mkdir(parents=True, exist_ok=True)

    print(f"Cloud cycle: uploading {len(pending)} unsent file(s)...")
    uploaded = 0
    for p in pending:
        rel = p.relative_to(source_root).as_posix()
        try:
            if use_local_dir and dest_root:
                _upload_to_local_dir(p, source_root, dest_root)
            else:
                remote_target = f"{args.cloud_rclone_remote.rstrip('/')}" \
                    f"/{args.cloud_rclone_base.strip('/')}" \
                    f"/{rel}"
                _upload_via_rclone(p, remote_target)
            _append_sent(sent_log, rel, p.stat().st_size)
            uploaded += 1
            print(f"Cloud upload OK: {rel}")
        except Exception as exc:  # noqa: BLE001
            print(f"Cloud upload FAILED: {rel} ({exc})")

    print(f"Cloud cycle complete: uploaded={uploaded}/{len(pending)}")
    return 0 if uploaded == len(pending) else 1
