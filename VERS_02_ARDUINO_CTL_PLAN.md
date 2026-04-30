# Vers_02_Arduino_CTL Plan

## Goal
Shift WiFi-window upload initiation to an Arduino-driven ready signal while keeping Python (`bsm_network`/`bsm_web`) in control of transfer orchestration.

## Proposed Change Scope (This Repository)
1. Arduino application protocol:
   - Add a new UDP readiness message (example): `READY_TO_UPLOAD,<uid>,<filename>,<size>,<ts>`.
   - Add ACK handling from controller (example): `ACK_READY,<uid>,<filename>`.
   - Add retry/backoff behavior for READY beacons until ACK or window end.
2. `bsm_network`:
   - Add listener/handler for READY beacons.
   - Queue READY devices and trigger existing transfer path (`LIST_FILES`/`START_FILE`) from queue.
   - Keep current pull/poll flows as fallback.
3. `bsm_web`:
   - Add visibility for READY queue state (optional first pass, recommended).
4. Logging/diagnostics:
   - Log READY received, ACK sent, transfer start, transfer result, and queue delay.
5. Backward compatibility:
   - Keep current command protocol intact so existing devices continue to work.

## Arduino Sequence (Deployment Sketch: `Arduino_WIFI.ino`)
Your proposed sequence is correct in structure. Suggested final wording:

1. Enter WiFi window.
2. Wait for reboot by user (`WiFi: reboot req`).
3. After reboot, run startup calibration capture sequence (not applicable to `Arduino_WIFI_Plain.ino`).
4. Run Analyze/Trim from raw `DL` file to `TR` file.
5. Establish network contact (WiFi + UDP listener ready).
6. Send `I'M_READY_TO_UPLOAD` beacon to controller.
7. Send file when requested by controller (`START_FILE` transfer flow).
8. Sync Arduino RTC with `bsm_network` clock.
9. Return to standby (not powered down) until end of WiFi window; continue periodic wake/listen behavior.

## Notes / Clarifications
1. Step 6 is new behavior for `Vers_02_Arduino_CTL` (not current `Vers_01` default behavior).
2. For field robustness, READY retries should include small jitter (for example 0–10 s).
3. Python-side AP concurrency limits remain the authority for simultaneous transfers.
4. `Arduino_WIFI_Plain.ino` should keep no-calibration/no-trim behavior and use synthetic files for pipeline testing.

## Suggested Implementation Order
1. Add READY/ACK protocol fields and parser support on Arduino.
2. Add READY listener + queue in `bsm_network`.
3. Add queue/ready observability in `bsm_web`.
4. Test with 1 device, then 6 concurrent-ready simulation, then full staged field rollout.
