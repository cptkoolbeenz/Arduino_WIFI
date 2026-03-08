# Cellular Commissioning Checklist (Gateway 4G)

## Scope
Commission cellular WAN for a field gateway that runs BSM Network services and uploads data to cloud.

## 1. Pre-Install
- Confirm LTE module model and supported LTE bands.
- Confirm carrier SIM plan allows router/IoT data use.
- Record SIM ICCID, APN, and SIM PIN status.
- Verify antenna kit (main/aux) matches module connectors.

## 2. Physical Install
- Power off gateway before inserting SIM.
- Install SIM correctly (orientation per gateway docs).
- Connect LTE main/aux antennas; torque finger-tight plus slight snug.
- Mount antennas outside enclosure when possible.
- Keep coax short and avoid tight bends.
- Bond metal enclosure and surge protection to site ground.

## 3. Gateway Cellular Configuration
- Set APN exactly as carrier provides.
- Configure SIM PIN only if required.
- Set network mode to `LTE preferred` (or LTE-only if stable at site).
- Enable cellular as WAN and confirm NAT for LAN clients.
- Configure DNS (carrier default or explicit servers).

## 4. Health and Recovery
- Enable watchdog with at least two ping targets (`1.1.1.1`, `8.8.8.8`).
- Set recovery sequence: modem reconnect/reset, then full reboot on repeated failures.
- Set registration/connectivity health interval (60-120 seconds).
- Enable persistent logs for reconnect and WAN state changes.

## 5. Signal Quality Acceptance Targets
Record values at final antenna position.

- `RSRP`: target better than `-100 dBm` (good: `-90 dBm` or better)
- `RSRQ`: target better than `-12 dB` (good: `-10 dB` or better)
- `SINR`: target above `5 dB` (good: `10 dB+`)
- `RSSI`: secondary indicator only

If values are below target, reposition antenna before proceeding.

## 6. Throughput and Latency Validation
- Run 3 upload/download tests at different times.
- Run 100-ping test to stable public host.
- Record average and worst-case latency.
- Verify upload capacity can clear expected daily backlog.

## 7. Data Budget Controls
- Enable monthly usage counter and threshold alert.
- Keep cloud upload schedule window configured (default `01:00-04:00`).
- Confirm retry/backoff policy for failed uploads.
- Disable or limit daytime bulk transfer unless needed.

## 8. BSM Application Validation
- Run discovery and verify Arduino visibility.
- Run file retrieval and verify local file/log creation.
- Run cloud one-shot upload test and verify destination.
- Confirm sent/unsent ledger behavior across retries.

## 9. Failure and Recovery Testing
- Temporarily remove WAN/antenna path and confirm watchdog recovery.
- Power-cycle gateway and verify auto-start of services.
- Confirm no duplicate uploads after reconnect.

## 10. Acceptance Record
- Date/time:
- Site ID:
- Technician:
- Gateway model/firmware:
- LTE module model/firmware:
- Carrier/APN (masked):
- Final signal metrics (RSRP/RSRQ/SINR):
- Throughput/latency summary:
- Watchdog settings:
- Upload window settings:
- Notes/issues:

## 11. Quick Troubleshooting
- No cellular registration: verify SIM seating, APN, band support, antenna connection.
- Weak/unstable signal: improve antenna placement, elevation, and grounding.
- Reconnect loops: verify APN auth, watchdog aggressiveness, and firmware version.
- Data overuse: tighten upload window and confirm unsent-file filtering.
