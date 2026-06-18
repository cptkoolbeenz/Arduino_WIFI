# Gateway Restore Guide

## Purpose

This document describes how to restore the Linovision reComputer R100x Gateway after hardware failure, SD/eMMC corruption, accidental deletion, or replacement of the Gateway hardware.

The backups stored in this directory contain:

* Arduino_WIFI application code
* BSM Network service
* BSM Web service
* UniFi Controller configuration
* Adopted AP configuration
* WiFi network settings
* Upload directories
* Local databases and configuration files

Backups: (these are found in Dropbox/Big Science/NORTH_END_WIFI)

* Arduino_WIFI_Backup_2026_06_18.tar.gz
* unifi_Backup_2026_06_18.tar.gz

---

# Assumptions

A replacement Gateway has:

* Raspberry Pi OS Bookworm installed
* Docker installed and functioning
* Tailscale installed (optional)
* User account: `recomputer`

---

# Restore Arduino_WIFI

Copy the backup archive to the Gateway:

```bash
scp Arduino_WIFI_Backup_2026_06_18.tar.gz recomputer@<gateway-ip>:~
```

SSH to the Gateway:

```bash
ssh recomputer@<gateway-ip>
```

Restore:

```bash
cd ~

tar -xzf Arduino_WIFI_Backup_2026_06_18.tar.gz
```

Verify:

```bash
ls ~/Arduino_WIFI
```

---

# Restore UniFi Configuration

Copy the archive:

```bash
scp unifi_Backup_2026_06_18.tar.gz recomputer@<gateway-ip>:~
```

Restore:

```bash
cd ~

tar -xzf unifi_Backup_2026_06_18.tar.gz
```

Verify:

```bash
ls ~/unifi
```

---

# Start UniFi Docker Container

Verify Docker is installed:

```bash
docker ps
```

If the UniFi container does not exist, recreate it using the deployment notes used during initial setup.

The container should mount:

```text
/home/recomputer/unifi
```

to:

```text
/unifi
```

inside the container.

Verify:

```bash
docker ps
```

Expected:

```text
unifi    Up (healthy)
```

---

# Restore BSM Services

Verify services exist:

```bash
systemctl status bsm-network
systemctl status bsm-web
```

Enable if necessary:

```bash
sudo systemctl enable bsm-network
sudo systemctl enable bsm-web
```

Start:

```bash
sudo systemctl start bsm-network
sudo systemctl start bsm-web
```

Verify:

```bash
systemctl is-active bsm-network
systemctl is-active bsm-web
```

Expected:

```text
active
active
```

---

# Verify Operation

Check:

```bash
sudo docker ps
```

Expected:

```text
unifi    Up (healthy)
```

Check Tailscale:

```bash
tailscale status
```

Check BSM Web:

Open browser:

```text
http://<gateway-ip>:5000
```

Confirm dashboard loads.

Check BSM Network:

```bash
journalctl -u bsm-network -f
```

Confirm Arduino uploads are being received.

---

# Notes

## SSD Issue (June 2026)

A FORESEE XP1000F128G NVMe SSD caused:

* NVMe I/O timeouts
* 40+ minute boot times
* Tailscale delays
* SSH delays

The SSD was removed.

The Gateway currently operates using internal eMMC storage only.

Do not reinstall the SSD unless the issue has been resolved.

---

## UniFi Configuration

The active UniFi controller runs in Docker.

The native Linux service:

```bash
unifi.service
```

has been disabled because it caused MongoDB listener conflicts and excessive log growth.

Verify:

```bash
systemctl is-enabled unifi
```

Expected:

```text
disabled
```
