# Linovision Gateway Troubleshooting Notes

## June 2026

### System

* Hardware: Linovision reComputer R100x
* OS: Raspberry Pi OS (Bookworm)
* UniFi Controller: Docker (`jacobalberty/unifi:latest`)
* BSM Network: systemd service
* BSM Web: systemd service
* Remote Access: Tailscale

---

# Issue #1: Extremely Slow Boot with NVMe SSD Installed

## Symptoms

After installing the supplied FORESEE XP1000F128G 128GB NVMe SSD:

* Gateway required approximately 40 minutes to become usable.
* Display initially showed the Raspberry Pi splash screen and then appeared frozen.
* SSH unavailable for 30–50 minutes.
* Tailscale unavailable during boot.
* Console displayed repeated kernel warnings:

```text
INFO: task swapper/0 blocked for more than 120 seconds
```

System analysis showed:

```text
Startup finished in 38min 56s (kernel) + 1min 54s (userspace)
```

Normal boot without SSD:

```text
Startup finished in 3.3s (kernel) + 1m 59s (userspace)
```

---

## Diagnostic Results

SSD detected:

```bash
lsblk
```

showed:

```text
nvme0n1 119.2G
```

Device information:

```bash
cat /sys/class/nvme/nvme0/model
```

returned:

```text
FORESEE XP1000F128G
```

Kernel log showed repeated NVMe timeouts:

```bash
sudo dmesg -T | grep -i nvme
```

Examples:

```text
nvme nvme0: I/O tag 98 QID 2 timeout, completion polled
nvme nvme0: I/O tag 111 QID 2 timeout, completion polled
```

The SSD was visible but was experiencing repeated I/O timeouts.

---

## Resolution

SSD was removed.

Results:

* Boot time returned to approximately 2 minutes.
* Tailscale connected normally.
* SSH available immediately after boot.
* Desktop available normally.

Current recommendation:

* Operate without the SSD.
* Store field data on internal eMMC storage.
* Await Linovision support response regarding SSD compatibility or replacement.

---

# Issue #2: UniFi MongoDB Log Growing Rapidly

## Symptoms

Available storage unexpectedly decreased.

Disk usage analysis showed:

```bash
du -sh /home/recomputer/unifi/log/*
```

Result:

```text
2.0G mongod.log
```

The MongoDB log was consuming approximately 2 GB.

---

## Diagnostic Results

Log contained repeated messages:

```text
Failed to set up listener:
SocketException: Address already in use

SERVER RESTARTED
```

repeating every few seconds.

Investigation showed two UniFi installations:

### Docker UniFi Controller

```bash
docker ps
```

```text
jacobalberty/unifi:latest
```

Ports:

```text
8080
8443
```

Active and healthy.

### Native Systemd UniFi

```bash
systemctl status unifi
```

Also running.

This resulted in competing MongoDB startup attempts.

---

## Verification

The native UniFi service was stopped:

```bash
sudo systemctl stop unifi
```

Immediately afterward:

```text
mongod.log stopped growing rapidly
```

and log messages changed to normal MongoDB operation:

```text
waiting for connections on port 27117
connection accepted from 127.0.0.1
```

Docker UniFi remained healthy:

```bash
docker ps
```

```text
Up 6 days (healthy)
```

---

## Resolution

Disable native UniFi service:

```bash
sudo systemctl disable unifi
```

Verification:

```bash
systemctl is-enabled unifi
```

returns:

```text
disabled
```

Docker restart policy:

```bash
docker inspect unifi --format='{{.HostConfig.RestartPolicy.Name}}'
```

returns:

```text
unless-stopped
```

Docker UniFi remains active and automatically starts after reboot.

---

# Storage Status

Current filesystem:

```text
/dev/mmcblk0p2
```

Free space after cleanup:

Approximately 13 GB.

Projected remaining field-season data:

Approximately 6 GB.

Current recommendation:

* Continue storing uploads on internal storage.
* Do not deploy SSD until NVMe issue is resolved.
* Periodically back up uploads and SQLite database.

---

# Additional Field Notes

A Tacuna Arduino failed to upload because it was connected to the wrong WiFi network.

Observed:

```text
SSID: HappyHappy
IP: 192.168.40.154
UDP target: 192.168.10.1:5005
```

The Arduino was connected to the home network rather than the UniFi field network.

Resolution:

Update `secrets.h`:

```cpp
#define SECRET_SSID "<Field WiFi SSID>"
#define SECRET_PASS "<Field WiFi Password>"
```

After reconnecting to the correct UniFi network, uploads should proceed normally.
