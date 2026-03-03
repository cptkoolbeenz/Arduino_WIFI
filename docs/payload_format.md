# Payload Format

Transport: UDP

Payload (CSV):
`device_id,unix_time,sample`

Example:
`MOM01,1739936401,842311`

Notes:
- Keep field order stable for Python parser compatibility.
- If schema changes, version it in this file.
