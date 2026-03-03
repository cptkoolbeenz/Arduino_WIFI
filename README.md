# Arduino_WIFI (UNO R4 WiFi)

Starter sketch for sending WiFi payloads from Arduino UNO R4 WiFi.

## Files
- `Arduino_WIFI.ino`: base firmware (WiFi + UDP send loop)
- `secrets.example.h`: template for WiFi and network settings
- `secrets.h`: local credentials/settings (not committed)

## Quick start
1. Copy `secrets.example.h` to `secrets.h`.
2. Edit `SECRET_SSID`, `SECRET_PASS`, and UDP target settings.
3. Open `Arduino_WIFI.ino` in Arduino IDE.
4. Select board: `Arduino UNO R4 WiFi`.
5. Select the correct port and upload.
6. Open Serial Monitor at `115200` to verify connection and packets.

## Payload format
Current payload is CSV:
`device_id,unix_time,sample`

Example:
`MOM01,1739936401,512`

`unix_time` is currently derived from `millis()/1000`. Replace with RTC/NTP when ready.

## Python listener (macOS)

From this repo directory:

```bash
cd ~/devel/Arduino_WIFI
python3 arduino_udp_listener.py --port 5005
```

If you want CSV logging:

```bash
python3 arduino_udp_listener.py --port 5005 --csv-log data/packets.csv
```

Notes:
- `--port` should match `UDP_TARGET_PORT` in `secrets.h`.
- Current sketch sends one UDP payload per second in CSV format:
  `device_id,unix_time,sample`
# Arduino_WIFI
