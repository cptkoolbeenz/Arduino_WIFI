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

## AirLift auto-recovery (AirLift profile only)

When `WIFI_PROFILE_AIRLIFT` is selected, the sketch self-heals a dead AirLift
on boot. After `SD.begin` succeeds, `checkAndMaybeFlashWiFi()` runs:

1. Probes `WiFi.firmwareVersion()` over SPI.
2. If the AirLift returns a sane string -> LCD shows `WiFi fw: 3.3.0`,
   sketch continues normally. A persistent retry counter (see below) is
   cleared on every healthy boot.
3. If the AirLift is unresponsive AND the SD card has `NINAFW.BIN` in the
   root, the R4 reflashes the ESP32 over its UART directly (MD5-verified),
   then `NVIC_SystemReset`s so the new firmware boots cleanly. ~150 s at
   921600 baud for a 1.33 MB nina-fw image.
4. A counter file `RECOVCNT.TXT` on the SD caps consecutive failed
   recoveries at 3 attempts. After the cap, recovery refuses to try again
   and shows `Recovery limit / hit - manual fix` on the LCD. Reset by
   deleting `RECOVCNT.TXT` from the SD card.

Set `WIFI_AUTO_RECOVERY` to `0` near the top of the sketch to disable.

### Fleet provisioning workflow

For a fleet of ~40 AirLift units the recommended provisioning workflow is:

1. **One-time per board: burn the ESP32 VDD_SDIO efuses to 3.3V.** Use
   `tools/efuse_burn/` from
   [TacunaSystems/Arduino_WIFI_AirLift](https://github.com/TacunaSystems/Arduino_WIFI_AirLift).
   Without this, WROOM-32 modules that don't ship factory-efused can latch
   the wrong flash voltage at boot, eventually corrupting the AirLift. The
   burn is permanent; ~10 s per unit. **Do this once before deploying.**
2. **First-flash provisioning per unit:** copy the latest
   `NINA_ADAFRUIT-esp32-X.Y.Z.bin` to the SD root as `NINAFW.BIN`, insert
   the SD, power on. On first boot the AirLift comes up factory-blank,
   the auto-recovery flow detects it and flashes from the card. The same
   path can be used to upgrade nina-fw in the field — just put a newer
   `NINAFW.BIN` on the SD and bump the chip into a bad state (or use
   `tools/brick_airlift/` for a controlled test).
3. **Field recovery (automatic):** once deployed, if the AirLift's
   firmware corrupts itself (brownout during flash, lightning event,
   flash wear), the next power cycle auto-recovers from the same
   `NINAFW.BIN` on the SD. No site visit needed. The 3-attempt cap
   prevents a wedged unit from chewing SD write endurance forever.

The `RECOVCNT.TXT` counter file is the only operational state added on
the SD card. The rest of the recovery is self-contained in the sketch.

## Notes

This sketch starts from the production root `Arduino_WIFI.ino` and adds the Tacuna/AirLift WiFi communication differences behind compile-time guards. The root production sketch is unchanged.
