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

For a fleet of ~40 AirLift units, one provisioning SD card can be used
to bring up the whole fleet sequentially:

1. **Prep one SD card:** copy two files to the SD root:
   - `NINA_ADAFRUIT-esp32-X.Y.Z.bin` renamed to `NINAFW.BIN` (~1.33 MB)
   - `EFUSE.OK` (empty file is fine — the sketch only checks for its existence)
2. **For each unit:** insert SD, power on. The auto-recovery flow:
   - probes nina-fw → fails (factory-blank AirLift)
   - sees `EFUSE.OK` → connects to ESP32 in flasher mode, **reads efuse state
     live from the chip**
   - if `XPD_SDIO_{REG,FORCE,TIEH}` not all set → burns them (forces
     VDD_SDIO=3.3V regardless of IO12 strap, equivalent to Espressif's
     factory burn) → resets to re-latch the strap → second boot reads the
     now-burned state and continues
   - if already burned (Adafruit factory boards or a previously-burned chip)
     → no-op on the burn → continues immediately
   - flashes `NINAFW.BIN`, MD5-verifies, resets
   - third boot (or second if burn was a no-op): nina-fw runs correctly,
     sketch enters normal operation
3. **Move SD to the next unit** and repeat. The sentinel-gated burn re-runs
   on each unit because **the source of truth is the chip's actual efuse
   state read live every boot**, not a marker file on the SD. After
   provisioning each unit it gets its own operational SD card.
4. **Field recovery (automatic):** once deployed, if the AirLift firmware
   corrupts itself (brownout during flash, lightning event, flash wear),
   the next power cycle auto-recovers from `NINAFW.BIN` on the SD. The
   3-attempt cap (`NINA_RECOVERY_MAX_ATTEMPTS`) prevents a wedged unit
   from chewing SD write endurance forever.

The sketch maintains two state files on the SD root:
- `RECOVCNT.TXT` — recovery attempt counter (cleared on every healthy boot)
- `EFUSE.DN` — most-recent efuse read result, written purely as a forensic
  log line. Does NOT gate burn behavior; the chip's live efuse state does.

Why sentinel-gated and not unconditional auto-burn: efuse writes are
**permanent and irreversible**. Without the `EFUSE.OK` sentinel the burn
code never runs, so a transient SPI failure or genuinely-flaky AirLift
can never trigger an accidental hardware mutation in the field. The
sentinel is a deliberate operator action at provisioning time. Set
`WIFI_AUTO_EFUSE_BURN` to `0` near the top of the sketch to disable the
burn path entirely.

## Notes

This sketch starts from the production root `Arduino_WIFI.ino` and adds the Tacuna/AirLift WiFi communication differences behind compile-time guards. The root production sketch is unchanged.
