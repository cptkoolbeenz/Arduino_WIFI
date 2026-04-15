from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

from .cloud import run_cloud_upload_cycle
from .db import init_db, log_scheduler_cycle
from .discovery import run_discovery


def is_between_hours(now: dt.datetime, start_hour: int, end_hour: int) -> bool:
    h = now.hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


def _parse_hhmm(value: str) -> int:
    raw = value.strip()
    if len(raw) != 4 or not raw.isdigit():
        raise ValueError(f"Invalid HHMM time: {value}")
    hh = int(raw[:2])
    mm = int(raw[2:])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"Invalid HHMM time: {value}")
    return hh * 60 + mm


def is_between_hhmm(now: dt.datetime, start_hhmm: str, end_hhmm: str) -> bool:
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    cur = now.hour * 60 + now.minute
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def run_scheduled(args: argparse.Namespace) -> int:
    if not args.discover and not args.cloud_enabled:
        print("Scheduled mode requires discovery and/or cloud window to be enabled.")
        return 2
    if args.discover and not (0 <= args.start_hour <= 23 and 0 <= args.end_hour <= 23):
        print("Invalid schedule hours. Use 0-23 for --start-hour and --end-hour.")
        return 2
    try:
        _parse_hhmm(args.cloud_start)
        _parse_hhmm(args.cloud_end)
    except ValueError as exc:
        print(str(exc))
        return 2

    in_discovery_prev = None
    in_cloud_prev = None
    discovery_cycle_count = 0
    cloud_cycle_count = 0
    db_enabled = bool(getattr(args, "db_log", True))
    db_path = Path(str(getattr(args, "db_path", "data/bsm_network.db"))).expanduser()
    if db_enabled:
        try:
            init_db(db_path)
        except Exception as exc:
            print(f"Warning: failed to initialize DB '{db_path}': {exc}")
            db_enabled = False
    schedule_ref = f"UTC{args.time_offset_hours:+g}h"
    print(
        f"Scheduled mode enabled: discovery={args.start_hour:02d}:00-{args.end_hour:02d}:00 "
        f"(every {args.cycle_interval_sec:.0f}s, ref={schedule_ref}), "
        f"cloud={args.cloud_start}-{args.cloud_end} "
        f"(every {args.cloud_cycle_interval_sec:.0f}s, ref={schedule_ref})"
    )
    print("Press Ctrl+C to stop.")

    try:
        while True:
            now_local = dt.datetime.now()
            now_ref = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=args.time_offset_hours)
            in_discovery = is_between_hours(now_ref, args.start_hour, args.end_hour)
            in_cloud = args.cloud_enabled and is_between_hhmm(now_ref, args.cloud_start, args.cloud_end)

            if in_discovery_prev is None or in_discovery != in_discovery_prev:
                state = "IN WINDOW" if in_discovery else "OUT OF WINDOW"
                print(
                    f"[{now_local.isoformat(timespec='seconds')}] Discovery schedule ({schedule_ref} "
                    f"{now_ref.strftime('%H:%M:%S')}): {state}"
                )
                in_discovery_prev = in_discovery
            if in_cloud_prev is None or in_cloud != in_cloud_prev:
                state = "IN WINDOW" if in_cloud else "OUT OF WINDOW"
                print(
                    f"[{now_local.isoformat(timespec='seconds')}] Cloud schedule ({schedule_ref} "
                    f"{now_ref.strftime('%H:%M:%S')}): {state}"
                )
                in_cloud_prev = in_cloud

            if in_discovery:
                discovery_cycle_count += 1
                print(f"[{now_local.isoformat(timespec='seconds')}] Starting discovery cycle #{discovery_cycle_count}")
                rc = run_discovery(args)
                if db_enabled:
                    log_scheduler_cycle(
                        db_path=db_path,
                        cycle_type="discovery",
                        cycle_index=discovery_cycle_count,
                        rc=rc,
                        message="Scheduled discovery cycle complete.",
                    )
                end_time = dt.datetime.now().isoformat(timespec="seconds")
                print(f"[{end_time}] Discovery cycle #{discovery_cycle_count} complete (rc={rc})")
                time.sleep(max(args.cycle_interval_sec, 1.0))
            elif in_cloud:
                cloud_cycle_count += 1
                print(f"[{now_local.isoformat(timespec='seconds')}] Starting cloud cycle #{cloud_cycle_count}")
                rc = run_cloud_upload_cycle(args)
                if db_enabled:
                    log_scheduler_cycle(
                        db_path=db_path,
                        cycle_type="cloud",
                        cycle_index=cloud_cycle_count,
                        rc=rc,
                        message="Scheduled cloud cycle complete.",
                    )
                end_time = dt.datetime.now().isoformat(timespec="seconds")
                print(f"[{end_time}] Cloud cycle #{cloud_cycle_count} complete (rc={rc})")
                time.sleep(max(args.cloud_cycle_interval_sec, 1.0))
            else:
                time.sleep(max(args.out_window_sleep_sec, 1.0))
    except KeyboardInterrupt:
        print("\nStopped scheduled mode.")
        return 0
