# Arduino_WIFI_Combined

Combined sketch intended to support both current WiFi hardware paths with a compile-time profile switch.

Open this folder in Arduino IDE:

```text
sketches/Arduino_WIFI_Combined/Arduino_WIFI_Combined.ino
```

## WiFi Profile Selection

At the top of `Arduino_WIFI_Combined.ino`, select exactly one profile:

```cpp
#define WIFI_PROFILE_R4_WIFI 1
// #define WIFI_PROFILE_AIRLIFT 1
```

Use `WIFI_PROFILE_R4_WIFI` for the current Uno R4 WiFi onboard ESP32-S3 radio using `WiFiS3`.

Use `WIFI_PROFILE_AIRLIFT` for the Tacuna/Adafruit AirLift Shield path using `WiFiNINA`. The AirLift profile also enables:

- AirLift pin definitions
- `WiFi.setPins(...)`
- a short delay before `udp.parsePacket()` for the WiFiNINA/AirLift packet timing quirk

Only one profile may be enabled at a time.

## Secrets

Copy `secrets.example.h` to `secrets.h` in this folder before compiling.

## Notes

This sketch starts from the production root `Arduino_WIFI.ino` and adds the Tacuna/AirLift WiFi communication differences behind compile-time guards. The root production sketch is unchanged.
