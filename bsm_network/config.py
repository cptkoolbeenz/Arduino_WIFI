from __future__ import annotations

import argparse

# Network defaults used by CLI flags below.
# Edit these in one place when moving to a different local network.
DEFAULT_BIND_IP = "192.168.1.8"
DEFAULT_DISCOVER_BROADCAST_IP = "192.168.1.255"
DEFAULT_LISTEN_PORT = 5005
DEFAULT_DISCOVER_PORT = 8888
DEFAULT_LOCAL_OFFSET = -5
DEFAULT_HOST_LABEL = "Mac"
DEFAULT_START_HOUR = 21
DEFAULT_END_HOUR = 23
DEFAULT_CLOUD_START = "0100"
DEFAULT_CLOUD_END = "0400"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UDP listener/controller for Arduino BSM payloads"
    )
    parser.set_defaults(
        discover=True,
        transfer_latest_file=True,
        sync_time=True,
        cloud_enabled=True,
    )
    parser.add_argument("--bind", default=DEFAULT_BIND_IP, help=f"Local interface/IP to bind (default: {DEFAULT_BIND_IP})")
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT, help=f"UDP port to listen on (default: {DEFAULT_LISTEN_PORT})")
    parser.add_argument("--buffer-size", type=int, default=2048, help="Max UDP datagram size in bytes (default: 2048)")
    parser.add_argument("--csv-log", default="", help="Optional CSV file path to append parsed packets")
    parser.add_argument("--ack", action="store_true", help="Reply to sender with a simple ACK message")
    parser.add_argument("--host-label", default=DEFAULT_HOST_LABEL, help=f"Name shown in startup output for the host machine (default: {DEFAULT_HOST_LABEL})")
    parser.add_argument("--discover", action="store_true", help="Broadcast poll request and print discovered Arduino unique IDs")
    parser.add_argument("--no-discover", action="store_false", dest="discover", help="Disable discovery mode")
    parser.add_argument("--discover-ip", default=DEFAULT_DISCOVER_BROADCAST_IP, help=f"Broadcast IP for discovery polls (default: {DEFAULT_DISCOVER_BROADCAST_IP})")
    parser.add_argument("--discover-port", type=int, default=DEFAULT_DISCOVER_PORT, help=f"UDP port Arduino listens on for discovery polls (default: {DEFAULT_DISCOVER_PORT})")
    parser.add_argument("--discover-timeout", type=float, default=20.0, help="Seconds to wait for discovery replies (default: 20.0)")
    parser.add_argument("--discover-attempts", type=int, default=8, help="How many poll broadcasts to send (default: 8)")
    parser.add_argument("--discover-interval", type=float, default=0.5, help="Seconds between poll broadcasts (default: 0.5)")
    parser.add_argument("--discover-csv", default="", help="Optional CSV path to write discovered device table")
    parser.add_argument("--sync-time", action="store_true", help="Sync Arduino RTC from controller time before retrieval")
    parser.add_argument("--no-sync-time", action="store_false", dest="sync_time", help="Disable RTC sync command")
    parser.add_argument(
        "--sync-time-only",
        action="store_true",
        help="Discover devices and run RTC sync only (no data/file retrieval)",
    )
    parser.add_argument(
        "--time-offset-hours",
        type=float,
        default=DEFAULT_LOCAL_OFFSET,
        help=f"Hours offset applied to controller epoch before SET_TIME (default: {DEFAULT_LOCAL_OFFSET})",
    )
    parser.add_argument("--download-command", default="DOWNLOAD_DATA", help="UDP command sent to each Arduino to trigger data burst (default: DOWNLOAD_DATA)")
    parser.add_argument("--download-lines", type=int, default=4, help="How many CSV payload lines to capture per Arduino (default: 4)")
    parser.add_argument("--download-timeout", type=float, default=120.0, help="Seconds to wait per Arduino when capturing payload lines (default: 120.0)")
    parser.add_argument("--post-poll-wait", type=float, default=10.0, help="Seconds to wait after polling before starting downloads (default: 10.0)")
    parser.add_argument("--download-dir", default="data", help="Directory for per-device downloaded CSV files (default: data)")
    parser.add_argument("--transfer-latest-file", action="store_true", help="After discovery, list remote files and transfer the latest unsaved file")
    parser.add_argument("--no-transfer-latest-file", action="store_false", dest="transfer_latest_file", help="Disable latest-file transfer after discovery")
    parser.add_argument(
        "--transfer-latest-even-if-seen",
        action="store_true",
        help="Download the most recent remote file even if it was already received before",
    )
    parser.add_argument("--file-list-timeout", type=float, default=10.0, help="Seconds to wait for LIST_FILES response per device (default: 10.0)")
    parser.add_argument("--file-output-dir", default="data/files", help="Directory for transferred files (default: data/files)")
    parser.add_argument("--file-log-dir", default="data/file_logs", help="Directory for per-device file transfer logs (default: data/file_logs)")
    parser.add_argument(
        "--transfer-tolerant",
        action="store_true",
        help="Allow partial file saves when EOF integrity checks fail (default: strict off)",
    )
    parser.add_argument(
        "--mark-partial-received",
        action="store_true",
        help="Treat partial transfers as received so they are not retried (only used with --transfer-tolerant)",
    )
    parser.add_argument("--scheduled", action="store_true", help="Run discover/transfer in a repeating time-window loop (opt-in)")
    parser.add_argument("--start-hour", type=int, default=DEFAULT_START_HOUR, help=f"Scheduled mode start hour, 0-23 local time (default: {DEFAULT_START_HOUR})")
    parser.add_argument("--end-hour", type=int, default=DEFAULT_END_HOUR, help=f"Scheduled mode end hour, 0-23 local time (default: {DEFAULT_END_HOUR})")
    parser.add_argument("--cycle-interval-sec", type=float, default=180.0, help="Seconds between cycles while inside schedule window (default: 180)")
    parser.add_argument("--out-window-sleep-sec", type=float, default=45.0, help="Seconds to sleep between time checks outside schedule window (default: 45)")
    parser.add_argument("--cloud-enabled", action="store_true", dest="cloud_enabled", help="Enable cloud upload window processing")
    parser.add_argument("--cloud-start", default=DEFAULT_CLOUD_START, help=f"Cloud upload window start in HHMM local time (default: {DEFAULT_CLOUD_START})")
    parser.add_argument("--cloud-end", default=DEFAULT_CLOUD_END, help=f"Cloud upload window end in HHMM local time (default: {DEFAULT_CLOUD_END})")
    parser.add_argument("--cloud-cycle-interval-sec", type=float, default=300.0, help="Seconds between cloud upload cycles in cloud window (default: 300)")
    parser.add_argument("--cloud-source-dir", default="data/files", help="Directory containing downloaded files to upload (default: data/files)")
    parser.add_argument("--cloud-sent-log", default="data/cloud_sent_files.csv", help="CSV ledger of files already sent to cloud (default: data/cloud_sent_files.csv)")
    parser.add_argument("--cloud-rclone-remote", default="", help="rclone remote name (required for actual upload), e.g. gdrive:")
    parser.add_argument("--cloud-rclone-base", default="BSM_Uploads", help="Remote base path/folder under rclone remote (default: BSM_Uploads)")
    parser.add_argument(
        "--cloud-local-dir",
        default="/Users/bobmauck/Library/CloudStorage/GoogleDrive-mauckr@kenyon.edu/.shortcut-targets-by-id/1paNSXGkj41CwPOn-VE1BFRt7k3oTzm51/PETREL NSF GRANT/2025 DATA and ANALYSIS/Bob Automation Information",
        help="Local destination directory for cloud sync clients (alternative to rclone remote)",
    )
    parser.add_argument("--cloud-once", action="store_true", help="Run one immediate cloud upload cycle and exit")
    return parser.parse_args()
