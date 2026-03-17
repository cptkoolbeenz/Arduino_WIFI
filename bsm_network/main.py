from __future__ import annotations

import datetime as dt
import socket
import sys
from pathlib import Path

from . import __version__
from .cloud import run_cloud_upload_cycle
from .config import parse_args
from .discovery import detect_lan_ip, run_discovery
from .protocol import parse_payload
from .records import append_csv
from .scheduler import run_scheduled


def main() -> int:
    args = parse_args()
    print(f"BSM Network {__version__}")

    csv_path = Path(args.csv_log).expanduser() if args.csv_log else None

    if args.cloud_once:
        return run_cloud_upload_cycle(args)

    if args.scheduled:
        return run_scheduled(args)

    if args.discover:
        return run_discovery(args)

    suggested_ip = detect_lan_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    requested_bind = (args.bind or "").strip() or "0.0.0.0"
    bound_bind = requested_bind
    try:
        sock.bind((requested_bind, args.port))
    except OSError as exc:
        if requested_bind != "0.0.0.0":
            sock.bind(("0.0.0.0", args.port))
            bound_bind = "0.0.0.0"
            print(
                f"Warning: bind to {requested_bind}:{args.port} failed ({exc}). "
                "Falling back to 0.0.0.0."
            )
        else:
            raise

    print(f"{args.host_label} UDP listener ready.")
    print(f"Listening on udp://{bound_bind}:{args.port}")
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
