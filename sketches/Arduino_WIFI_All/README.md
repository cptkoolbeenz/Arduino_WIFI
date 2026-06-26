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

1. **Prep one SD card:** copy `NINAFW.BIN` to the SD root. Use the copy
   shipped with this PR at `libraries/ESPSerialFlasher/extras/NINAFW.BIN`
   (1,333,248 bytes, MD5 `7b50dfbc97f488fce09e49a3bd8c779c`). This is the
   modernized `nina-fw 3.3.0` fork validated end-to-end with the
   auto-recovery flasher. **Do not substitute a different build** —
   older Adafruit stock images flash and verify but won't speak the
   WiFiNINA SPI protocol the way this sketch expects, so the recovery
   loop will appear to succeed (flash OK, MD5 verified) but post-reboot
   `firmwareVersion()` will keep returning garbage and the unit will hit
   the 3/3 retry cap.
2. **For each unit:** insert SD, power on. The auto-recovery flow:
   - probes nina-fw → fails (factory-blank AirLift)
   - connects to ESP32 in flasher mode, reads efuse state live from
     the chip
   - if `XPD_SDIO_{REG,FORCE,TIEH}` not all set → burns them (forces
     VDD_SDIO=3.3V regardless of IO12 strap, equivalent to Espressif's
     factory burn) → resets to re-latch the strap → second boot reads
     the now-burned state and continues
   - if already burned (Adafruit factory boards or a previously-burned
     chip) → no-op on the burn → continues immediately
   - flashes `NINAFW.BIN`, MD5-verifies, resets
   - next boot: nina-fw runs correctly, sketch enters normal operation
3. **Move SD to the next unit** and repeat. The efuse check re-runs on
   each unit because the source of truth is the chip's actual efuse
   state read live every boot. After provisioning each unit it gets
   its own operational SD card.
4. **Field recovery (automatic):** once deployed, if the AirLift firmware
   corrupts itself (brownout during flash, lightning event, flash wear),
   the next power cycle auto-recovers from `NINAFW.BIN` on the SD. The
   3-attempt cap (`NINA_RECOVERY_MAX_ATTEMPTS`) prevents a wedged unit
   from chewing SD write endurance forever.

The sketch maintains two state files on the SD root:
- `RECOVCNT.TXT` — recovery attempt counter (cleared on every healthy boot)
- `EFUSE.DN` — most-recent efuse read result, written purely as a forensic
  log line. Does NOT gate behavior; the chip's live efuse state does.

Why the burn fires automatically (no sentinel file): the burn only runs
when ALL of (a) `firmwareVersion()` returns garbage, (b) SD is ready
with a valid `NINAFW.BIN`, (c) ESPFlasher syncs successfully to the chip,
and (d) the chip's live efuse read shows the bits unset are true together.
Under those four conditions a chip is unambiguously in need of the burn —
withholding it just leaves the unit in a known-broken state. Earlier
versions required an `EFUSE.OK` sentinel file on the SD as operator
consent, but in practice that just meant operators forgot to create it
and the recovery never burned anything (the exact failure that led to
this design). Set `WIFI_AUTO_EFUSE_BURN` to `0` at the top of the sketch
to disable the burn path entirely if you ever want the old behavior.

## Notes

This sketch starts from the production root `Arduino_WIFI.ino` and adds the Tacuna/AirLift WiFi communication differences behind compile-time guards. The root production sketch is unchanged.
