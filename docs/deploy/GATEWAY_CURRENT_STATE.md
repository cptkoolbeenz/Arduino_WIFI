# Gateway Current State

Snapshot of the known-good Gateway configuration after the latest tested web
and file-transfer changes.

## Date Tested

- 2026-06-18

## Gateway Identity

- Gateway hostname: `reComputer-R100x`
- Gateway Tailscale IP: `100.95.252.40`
- Gateway LAN IP observed during setup: `192.168.40.111`
- Gateway field/AP-side interface: `eth1`
- Gateway field/AP-side IP: `192.168.10.1/24`
- DHCP service on field network: `dnsmasq`
- DHCP lease range observed during setup: `192.168.10.50` to `192.168.10.150`

## AP Names And SSIDs

- Active network profile: `field`
- Gateway/web host label: `NORTH_END_IOT`
- Arduino WiFi SSID in current local `secrets.h`: `NORTH_END_IOT`
- Active AP routing map file: `docs/network_map.dual_mode.json`
- Active AP name in current map: `AP_HOME`
- Active AP transfer limit: `AP_HOME = 2`
- Default AP bucket: `AP_HOME`
- Fallback AP bucket limit: `DEFAULT = 1`

Field AP names retained in the map for manual field profile switching:

- `AP_FIELD_A`
- `AP_FIELD_B`
- `AP_FIELD_C`

## Docker

- Docker is installed and used for the UniFi Controller.
- Docker container name: `unifi`
- Docker image observed in troubleshooting notes: `jacobalberty/unifi:latest`
- Expected Docker state: `unifi    Up (healthy)`
- Docker restart policy: `unless-stopped`
- UniFi data mount on host: `/home/recomputer/unifi`
- UniFi data mount in container: `/unifi`
- BSM operation is systemd-managed, not Docker-managed.

Native UniFi service state:

- `unifi.service` should be disabled.
- Reason: native UniFi conflicted with Docker UniFi/MongoDB and caused rapid log growth.

## Important Services

- `bsm-network.service`: runs discovery, daily operations, and file retrieval.
- `bsm-web.service`: serves the web dashboard and File Transfers page.
- `bsm-healthcheck.service`: one-shot health check.
- `bsm-healthcheck.timer`: schedules periodic health checks.
- `tailscaled.service`: Tailscale remote access.
- `dnsmasq.service`: DHCP service for the field/AP-side network on `eth1`.
- `unifi.service`: native UniFi service; expected state is disabled.

Systemd unit files tracked in this repo:

- `deploy/systemd/bsm-network.service`
- `deploy/systemd/bsm-web.service`
- `deploy/systemd/bsm-healthcheck.service`
- `deploy/systemd/bsm-healthcheck.timer`

## Web/App State

- Web app name: `NORTH_END_IOT`
- Web app version: `4.4`
- Web bind host: `0.0.0.0`
- Web port: `5001`
- Config file: `config/network_profile.json`
- Database path: `data/bsm_network.db`
- Discovered-device CSV: `data/discovered_devices.csv`
- Gateway-saved file root: `data/files`
- Gateway storage currently used for uploads: internal eMMC.
- SSD/NVMe status: do not deploy SSD until NVMe timeout issue is resolved.

Known-good File Transfers behavior:

- Selecting a Known Arduino loads Gateway-saved files from `data/files/<short_uid>`.
- Gateway-saved files can be downloaded even when the Arduino is offline.
- Offline Arduino SD listing shows a short `<short_uid> is offline` message.
- The SD-file and Gateway-file panels remain side by side and the same size.

Known-good iPhone field page behavior:

- `/iphone` shows a compact static field status page.
- The page answers whether Arduinos are alive and whether files are being received.
- The page does not auto-refresh.
- The `Poll` button runs discovery-only polling and returns to the iPhone page.

## Arduino Sketch State

- Current combined sketch: `sketches/Arduino_WIFI_All/Arduino_WIFI_All.ino`
- Current Tacuna/AirLift/data build version string: `4.0ctd`
- WiFiNINA/AirLift file chunk size: `1024`
- R4 WiFi file chunk size: `4096`
- Acquisition filename is now created in `setup()` and is not rolled over in `loop()`.
- Acquisition rows remain:

```text
sensor_value, unix_time
```

## Useful Gateway Checks

```bash
hostname
tailscale ip -4
tailscale status
ip addr show eth1
systemctl status tailscaled --no-pager
systemctl status dnsmasq --no-pager
systemctl status bsm-network.service bsm-web.service --no-pager
systemctl status bsm-healthcheck.timer --no-pager
sudo docker ps
docker inspect unifi --format='{{.HostConfig.RestartPolicy.Name}}'
systemctl is-enabled unifi
```

## Updating Code On Gateway

After pushing from the Mac:

```bash
cd ~/Arduino_WIFI
git pull
python3 -m py_compile bsm_web.py bsm_network.py
sudo systemctl restart bsm-web.service
sudo systemctl restart bsm-network.service
sudo systemctl status bsm-web.service bsm-network.service --no-pager
```

If only `bsm_web.py` changed, restarting `bsm-web.service` is enough.

## Snapshot Notes

- This file was updated from the repo configuration, existing deployment notes,
  and the latest user-confirmed Gateway test.
- Runtime commands run from this local Mac identified the local host as
  `Roberts-Air.lan`, so Gateway-only runtime values were not re-queried directly
  from this machine during the file update.
