from __future__ import annotations

import argparse
import datetime as dt
import time

from .discovery import run_discovery


def is_between_hours(now: dt.datetime, start_hour: int, end_hour: int) -> bool:
    h = now.hour
    if start_hour == end_hour:
        return True
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


def run_scheduled(args: argparse.Namespace) -> int:
    if not args.discover:
        print("Scheduled mode requires --discover.")
        return 2
    if not (0 <= args.start_hour <= 23 and 0 <= args.end_hour <= 23):
        print("Invalid schedule hours. Use 0-23 for --start-hour and --end-hour.")
        return 2

    in_window_prev = None
    cycle_count = 0
    print(
        f"Scheduled mode enabled: window={args.start_hour:02d}:00-"
        f"{args.end_hour:02d}:00 local, cycle_interval={args.cycle_interval_sec:.0f}s"
    )
    print("Press Ctrl+C to stop.")

    try:
        while True:
            now = dt.datetime.now()
            in_window = is_between_hours(now, args.start_hour, args.end_hour)

            if in_window_prev is None or in_window != in_window_prev:
                state = "IN WINDOW" if in_window else "OUT OF WINDOW"
                print(f"[{now.isoformat(timespec='seconds')}] Schedule state: {state}")
                in_window_prev = in_window

            if in_window:
                cycle_count += 1
                print(f"[{now.isoformat(timespec='seconds')}] Starting cycle #{cycle_count}")
                rc = run_discovery(args)
                end_time = dt.datetime.now().isoformat(timespec="seconds")
                print(f"[{end_time}] Cycle #{cycle_count} complete (rc={rc})")
                time.sleep(max(args.cycle_interval_sec, 1.0))
            else:
                time.sleep(max(args.out_window_sleep_sec, 1.0))
    except KeyboardInterrupt:
        print("\nStopped scheduled mode.")
        return 0
