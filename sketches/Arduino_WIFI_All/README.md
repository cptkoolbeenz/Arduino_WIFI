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

1. **SD prep (once, scriptable for all 40 cards):** copy these two files to
   each SD root:
   - `NINA_ADAFRUIT-esp32-X.Y.Z.bin` renamed to `NINAFW.BIN` (~1.33 MB)
   - `EFUSE.OK` (empty file is fine — the sketch only checks for its existence)
2. **First boot per unit:** insert SD, power on. The auto-recovery flow:
   - probes nina-fw → fails (factory-blank AirLift)
   - detects `EFUSE.OK` sentinel and absent `EFUSE.DN` marker
   - connects to ESP32 in flasher mode, reads efuse state
   - if XPD_SDIO bits unset → burns them (forces VDD_SDIO=3.3V regardless of
     IO12 strap, equivalent to Espressif's factory burn) → writes `EFUSE.DN`
     marker → resets to re-latch the strap
   - if already burned (e.g. Adafruit factory boards) → just writes `EFUSE.DN`
     and continues
   - flashes `NINAFW.BIN`, MD5-verifies, resets
   - second boot: nina-fw runs correctly, sketch enters normal operation
3. **Field recovery (automatic):** once deployed, if the AirLift firmware
   corrupts itself (brownout during flash, lightning event, flash wear),
   the next power cycle auto-recovers from the same `NINAFW.BIN` on the SD.
   The sentinel-gated efuse code never runs again because `EFUSE.DN` is
   present from provisioning. No site visit needed. The 3-attempt cap
   (`NINA_RECOVERY_MAX_ATTEMPTS`) prevents a wedged unit from chewing SD
   write endurance forever.

The sketch creates two state files on the SD root:
- `RECOVCNT.TXT` — recovery attempt counter (cleared on every healthy boot)
- `EFUSE.DN` — efuse-burn-done marker (written once at provisioning;
  presence makes the burn check a no-op on every subsequent boot)

Why sentinel-gated and not unconditional auto-burn: efuse writes are
**permanent and irreversible**. Without the `EFUSE.OK` sentinel the burn
code never runs, so a transient SPI failure or genuinely-flaky AirLift
can never trigger an accidental hardware mutation in the field. The
sentinel is removed once `EFUSE.DN` exists — there is no path to a
second burn on a deployed unit. Set `WIFI_AUTO_EFUSE_BURN` to `0` near
the top of the sketch to disable the burn path entirely.

## Notes

This sketch starts from the production root `Arduino_WIFI.ino` and adds the Tacuna/AirLift WiFi communication differences behind compile-time guards. The root production sketch is unchanged.
