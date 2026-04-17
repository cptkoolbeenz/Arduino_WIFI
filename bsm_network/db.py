from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

UPSERT_DEVICE_SQL = """
INSERT INTO devices (
  unique_id, short_uid, network_uid, firmware_version, network_hostname, wifi_mac,
  device_ip, recv_ip, udp_target_ip, udp_target_port,
  ap_id, ap_source, burrow_id, short_uid_collision, short_uid_collision_note, last_seen, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(unique_id) DO UPDATE SET
  short_uid=excluded.short_uid,
  network_uid=excluded.network_uid,
  firmware_version=excluded.firmware_version,
  network_hostname=excluded.network_hostname,
  wifi_mac=excluded.wifi_mac,
  device_ip=excluded.device_ip,
  recv_ip=excluded.recv_ip,
  udp_target_ip=excluded.udp_target_ip,
  udp_target_port=excluded.udp_target_port,
  ap_id=excluded.ap_id,
  ap_source=excluded.ap_source,
  burrow_id=excluded.burrow_id,
  short_uid_collision=excluded.short_uid_collision,
  short_uid_collision_note=excluded.short_uid_collision_note,
  last_seen=excluded.last_seen,
  updated_at=excluded.updated_at
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
              unique_id TEXT PRIMARY KEY,
              short_uid TEXT,
              network_uid TEXT,
              firmware_version TEXT,
              network_hostname TEXT,
              wifi_mac TEXT,
              device_ip TEXT,
              recv_ip TEXT,
              udp_target_ip TEXT,
              udp_target_port INTEGER,
              ap_id TEXT,
              ap_source TEXT,
              burrow_id TEXT,
              short_uid_collision INTEGER DEFAULT 0,
              short_uid_collision_note TEXT,
              last_seen TEXT,
              updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS discovery_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              event_ts TEXT NOT NULL,
              unique_id TEXT,
              network_uid TEXT,
              device_ip TEXT,
              recv_ip TEXT,
              ap_id TEXT,
              ap_source TEXT,
              burrow_id TEXT,
              status TEXT NOT NULL,
              message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_discovery_events_run_id ON discovery_events(run_id);
            CREATE INDEX IF NOT EXISTS idx_discovery_events_ts ON discovery_events(event_ts);

            CREATE TABLE IF NOT EXISTS transfer_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              event_ts TEXT NOT NULL,
              unique_id TEXT,
              network_uid TEXT,
              burrow_id TEXT,
              ap_id TEXT,
              device_ip TEXT,
              source_filename TEXT,
              saved_path TEXT,
              status TEXT NOT NULL,
              message TEXT,
              error_text TEXT,
              duration_s REAL
            );
            CREATE INDEX IF NOT EXISTS idx_transfer_events_run_id ON transfer_events(run_id);
            CREATE INDEX IF NOT EXISTS idx_transfer_events_ts ON transfer_events(event_ts);

            CREATE TABLE IF NOT EXISTS slot_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              event_ts TEXT NOT NULL,
              unique_id TEXT,
              ap_id TEXT,
              action TEXT NOT NULL,
              running_total INTEGER NOT NULL,
              running_on_ap INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slot_events_run_id ON slot_events(run_id);
            CREATE INDEX IF NOT EXISTS idx_slot_events_ts ON slot_events(event_ts);

            CREATE TABLE IF NOT EXISTS scheduler_cycles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_ts TEXT NOT NULL,
              cycle_type TEXT NOT NULL,
              cycle_index INTEGER,
              rc INTEGER,
              message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scheduler_cycles_ts ON scheduler_cycles(event_ts);

            CREATE TABLE IF NOT EXISTS active_transfers (
              unique_id TEXT PRIMARY KEY,
              run_id TEXT,
              started_at TEXT NOT NULL,
              ap_id TEXT,
              device_ip TEXT,
              source_filename TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_active_transfers_started_at ON active_transfers(started_at);
            """
        )
        # Lightweight migration path for older DB files.
        for col_def in (
            "short_uid TEXT",
            "short_uid_collision INTEGER DEFAULT 0",
            "short_uid_collision_note TEXT",
            "firmware_version TEXT",
        ):
            try:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def upsert_device(db_path: Path, row: dict[str, str | int]) -> None:
    event_ts = _now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            UPSERT_DEVICE_SQL,
            (
                str(row.get("unique_id", "")),
                str(row.get("short_uid", "")),
                str(row.get("network_uid", "")),
                str(row.get("firmware_version", "")),
                str(row.get("network_hostname", "")),
                str(row.get("wifi_mac", "")),
                str(row.get("device_ip", "")),
                str(row.get("recv_ip", "")),
                str(row.get("udp_target_ip", "")),
                int(row.get("udp_target_port", 0) or 0),
                str(row.get("ap_id", "")),
                str(row.get("ap_source", "")),
                str(row.get("burrow_id", "")),
                int(row.get("short_uid_collision", 0) or 0),
                str(row.get("short_uid_collision_note", "")),
                str(row.get("last_seen", "")),
                event_ts,
            ),
        )


def self_test_db_writes(db_path: Path) -> tuple[bool, str]:
    """
    Validate that core write statements prepare and execute against this DB schema.
    Uses a transaction that is rolled back, so no persistent rows are added.
    """
    ts = _now_iso()
    params = (
        "__DB_SELFTEST__",
        "SELF00",
        "selftest-network",
        "1.0-test",
        "selftest-host",
        "001122AABBCC",
        "127.0.0.1",
        "127.0.0.1",
        "127.0.0.1",
        8888,
        "DEFAULT",
        "default",
        "BURROW_SELF",
        0,
        "",
        ts,
        ts,
    )
    try:
        with _connect(db_path) as conn:
            conn.execute("BEGIN")
            conn.execute(UPSERT_DEVICE_SQL, params)
            conn.execute("ROLLBACK")
        return True, ""
    except Exception as exc:
        return False, str(exc)


def log_discovery_event(
    db_path: Path,
    run_id: str,
    status: str,
    row: dict[str, str | int] | None = None,
    message: str = "",
) -> None:
    row = row or {}
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO discovery_events (
              run_id, event_ts, unique_id, network_uid, device_ip, recv_ip,
              ap_id, ap_source, burrow_id, status, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _now_iso(),
                str(row.get("unique_id", "")),
                str(row.get("network_uid", "")),
                str(row.get("device_ip", "")),
                str(row.get("recv_ip", "")),
                str(row.get("ap_id", "")),
                str(row.get("ap_source", "")),
                str(row.get("burrow_id", "")),
                status,
                message,
            ),
        )


def log_transfer_event(db_path: Path, run_id: str, result: dict[str, str | float]) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO transfer_events (
              run_id, event_ts, unique_id, network_uid, burrow_id, ap_id, device_ip,
              source_filename, saved_path, status, message, error_text, duration_s
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _now_iso(),
                str(result.get("unique_id", "")),
                str(result.get("network_uid", "")),
                str(result.get("burrow_id", "")),
                str(result.get("ap_id", "")),
                str(result.get("device_ip", "")),
                str(result.get("source_filename", "")),
                str(result.get("saved_path", "")),
                str(result.get("status", "")),
                str(result.get("message", "")),
                str(result.get("error_text", "")),
                float(result.get("duration_s", 0.0) or 0.0),
            ),
        )


def log_slot_event(
    db_path: Path,
    run_id: str,
    unique_id: str,
    ap_id: str,
    action: str,
    running_total: int,
    running_on_ap: int,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO slot_events (
              run_id, event_ts, unique_id, ap_id, action, running_total, running_on_ap
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, _now_iso(), unique_id, ap_id, action, running_total, running_on_ap),
        )


def log_scheduler_cycle(
    db_path: Path,
    cycle_type: str,
    cycle_index: int,
    rc: int,
    message: str = "",
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scheduler_cycles (
              event_ts, cycle_type, cycle_index, rc, message
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (_now_iso(), cycle_type, cycle_index, rc, message),
        )


def read_devices_snapshot(db_path: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT
              unique_id, short_uid, network_uid, firmware_version, network_hostname, wifi_mac, device_ip, recv_ip,
              udp_target_ip, udp_target_port, ap_id, ap_source, burrow_id,
              short_uid_collision, short_uid_collision_note, last_seen
            FROM devices
            ORDER BY COALESCE(last_seen, ''), unique_id
            """
        )
        rows = cur.fetchall()
    out: list[dict[str, str]] = []
    for r in rows:
        out.append(
            {
                "unique_id": str(r[0] or ""),
                "short_uid": str(r[1] or ""),
                "network_uid": str(r[2] or ""),
                "firmware_version": str(r[3] or ""),
                "network_hostname": str(r[4] or ""),
                "wifi_mac": str(r[5] or ""),
                "device_ip": str(r[6] or ""),
                "recv_ip": str(r[7] or ""),
                "udp_target_ip": str(r[8] or ""),
                "udp_target_port": str(r[9] or ""),
                "ap_id": str(r[10] or ""),
                "ap_source": str(r[11] or ""),
                "burrow_id": str(r[12] or ""),
                "short_uid_collision": str(r[13] or 0),
                "short_uid_collision_note": str(r[14] or ""),
                "last_seen": str(r[15] or ""),
            }
        )
    return out


def find_unique_ids_by_short_uid(db_path: Path, short_uid: str) -> list[str]:
    token = (short_uid or "").strip().upper()
    if not token or not db_path.exists():
        return []
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT unique_id FROM devices WHERE short_uid = ? ORDER BY unique_id",
            (token,),
        )
        rows = cur.fetchall()
    return [str(r[0] or "") for r in rows if str(r[0] or "").strip()]


def set_burrow_id_by_short_uid(db_path: Path, short_uid: str, burrow_id: str) -> tuple[bool, str]:
    token = (short_uid or "").strip().upper()
    if not token:
        return False, "short_uid is required."
    if not db_path.exists():
        return False, f"DB not found: {db_path}"

    burrow_clean = (burrow_id or "").strip()
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "SELECT unique_id FROM devices WHERE short_uid = ? ORDER BY unique_id",
                (token,),
            )
            rows = [str(r[0] or "").strip() for r in cur.fetchall() if str(r[0] or "").strip()]
            if not rows:
                return False, f"No device found with short_uid '{token}'."
            if len(rows) > 1:
                return (
                    False,
                    f"Cannot assign burrow_id: short_uid '{token}' is not unique ({', '.join(rows)}).",
                )

            uid = rows[0]
            conn.execute(
                "UPDATE devices SET burrow_id = ?, updated_at = ? WHERE unique_id = ?",
                (burrow_clean, _now_iso(), uid),
            )
            if burrow_clean:
                return True, f"Assigned burrow_id '{burrow_clean}' to short_uid '{token}' ({uid})."
            return True, f"Cleared burrow_id for short_uid '{token}' ({uid})."
    except sqlite3.OperationalError as exc:
        return False, f"DB update failed: {exc}"


def set_burrow_id_by_unique_id(db_path: Path, unique_id: str, burrow_id: str) -> tuple[bool, str]:
    uid = (unique_id or "").strip()
    if not uid:
        return False, "unique_id is required."
    if not db_path.exists():
        return False, f"DB not found: {db_path}"

    burrow_clean = (burrow_id or "").strip()
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "SELECT 1 FROM devices WHERE unique_id = ? LIMIT 1",
                (uid,),
            )
            found = cur.fetchone() is not None
            if not found:
                return False, f"No device found with unique_id '{uid}'."
            conn.execute(
                "UPDATE devices SET burrow_id = ?, updated_at = ? WHERE unique_id = ?",
                (burrow_clean, _now_iso(), uid),
            )
            if burrow_clean:
                return True, f"Assigned burrow_id '{burrow_clean}' to {uid}."
            return True, f"Cleared burrow_id for {uid}."
    except sqlite3.OperationalError as exc:
        return False, f"DB update failed: {exc}"


def set_transfer_active(
    db_path: Path,
    unique_id: str,
    run_id: str,
    ap_id: str,
    device_ip: str,
    source_filename: str,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO active_transfers (
              unique_id, run_id, started_at, ap_id, device_ip, source_filename
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(unique_id) DO UPDATE SET
              run_id=excluded.run_id,
              started_at=excluded.started_at,
              ap_id=excluded.ap_id,
              device_ip=excluded.device_ip,
              source_filename=excluded.source_filename
            """,
            (unique_id, run_id, _now_iso(), ap_id, device_ip, source_filename),
        )


def clear_transfer_active(db_path: Path, unique_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM active_transfers WHERE unique_id = ?", (unique_id,))


def is_transfer_active(db_path: Path, unique_id: str) -> bool:
    token = (unique_id or "").strip()
    if not token or not db_path.exists():
        return False
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "SELECT 1 FROM active_transfers WHERE unique_id = ? LIMIT 1",
                (token,),
            )
            row = cur.fetchone()
        return row is not None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise


def list_active_transfers(db_path: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        return []
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                """
                SELECT
                  COALESCE(unique_id, ''),
                  COALESCE(run_id, ''),
                  COALESCE(started_at, ''),
                  COALESCE(ap_id, ''),
                  COALESCE(device_ip, ''),
                  COALESCE(source_filename, '')
                FROM active_transfers
                ORDER BY started_at ASC
                """
            )
            rows = cur.fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise

    out: list[dict[str, str]] = []
    for r in rows:
        out.append(
            {
                "unique_id": str(r[0] or ""),
                "run_id": str(r[1] or ""),
                "started_at": str(r[2] or ""),
                "ap_id": str(r[3] or ""),
                "device_ip": str(r[4] or ""),
                "source_filename": str(r[5] or ""),
            }
        )
    return out
