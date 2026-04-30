# Payload Format

## Data Payload (listener mode)

Transport: UDP

Payload (CSV):
`device_id,unix_time,sample`

Example:
`MOM01,1739936401,842311`

Notes:
- Keep field order stable for Python parser compatibility.
- If schema changes, version it in this file.

## Control Plane (Vers_03_Arduino_CTL)

This section defines Arduino->controller readiness signaling for upload orchestration.

### READY Beacon (Arduino -> bsm_network)

Transport: UDP (to controller listener endpoint, typically `:5005`)

Format:
`READY_TO_UPLOAD,<unique_id>,<filename>,<size_bytes>,<unix_time>`

Field definitions:
- `unique_id`: Full MCU ID string used by discovery/device table.
- `filename`: Candidate upload file (for deployment path typically `TRYYMMDD.TXT`; plain-mode testing may vary).
- `size_bytes`: File size in bytes from SD metadata (or `0` if unknown at beacon time).
- `unix_time`: Arduino RTC unix time when beacon was sent.

Example:
`READY_TO_UPLOAD,31011F0B383136326B7F33354B573355,TR260430.TXT,4194304,1777545600`

### READY ACK (bsm_network -> Arduino)

Transport: UDP reply to Arduino control port (typically `:8888`)

Format:
`ACK_READY,<unique_id>,<filename>`

Example:
`ACK_READY,31011F0B383136326B7F33354B573355,TR260430.TXT`

### Behavioral Contract

1. Arduino sends `READY_TO_UPLOAD` once file is prepared and WiFi command mode is available.
2. `bsm_network` records readiness and ACKs with `ACK_READY`.
3. `bsm_network` remains transfer orchestrator and uses existing file flow (`LIST_FILES`, `START_FILE`, resume/tolerant logic).
4. Arduino should retry `READY_TO_UPLOAD` with backoff/jitter until ACK or window end.

### Retry/Backoff Guidance

- Initial retry interval: `30-60s`.
- Add small jitter (`0-10s`) to avoid lockstep bursts.
- Stop retries when:
  - `ACK_READY` received for same `unique_id` + `filename`, or
  - WiFi window ends.

### Backward Compatibility

- Existing control commands remain valid and unchanged:
  - `POLL_UID`, `LIST_FILES`, `START_FILE`, `GET_*`, `SET_TIME`, `REBOOT`, etc.
- `READY_TO_UPLOAD` is additive; legacy devices without READY support continue using pull/poll workflows.
