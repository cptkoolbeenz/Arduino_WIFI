# Headless Gateway Access Playbook

This document summarizes headless access options for the gateway and provides a recommended startup path for three operating scenarios.

## Scope
- Gateway host: `reComputer-R100x`
- Local control LAN: `192.168.10.0/24`
- Gateway local/admin IP: `192.168.10.1` (via `eth1` local side)
- Home-lab WAN-side example IP: `192.168.1.6` (via `eth0` uplink side)
- `bsm_web` default port: `5001`

## Key Principle
Headless does **not** require internet. It requires a local management path.

You can manage the gateway headlessly by:
- Local WiFi/LAN access to `192.168.10.1`
- Direct Ethernet fallback to local LAN side (`eth1`)
- SSH + `bsm_web` from local network

Remote-from-anywhere access (outside local subnet) requires VPN/tunnel and thus some active uplink.

## Headless Access Options

## 1) Local WiFi/LAN (primary)
- Connect laptop to local AP network (`192.168.10.x`)
- SSH: `ssh recomputer@192.168.10.1`
- Web UI: `http://192.168.10.1:5001`

## 2) Home/office uplink side (`eth0`)
When gateway WAN/uplink side is connected to another router and gets a routable LAN IP (example `192.168.1.6`):
- SSH: `ssh recomputer@192.168.1.6`
- Web UI: `http://192.168.1.6:5001`

## 3) Direct Ethernet fallback (no WiFi/internet)
If AP/uplink are unavailable:
- Connect laptop directly to gateway local side (`eth1` path)
- Set laptop static IP, for example: `192.168.10.50/24`
- SSH: `ssh recomputer@192.168.10.1`
- Web UI: `http://192.168.10.1:5001`

## 4) VPN/tunnel (remote access from anywhere)
- Recommended: Tailscale (or WireGuard)
- Requires active uplink (home router, lab WiFi, Starlink, etc.)
- Without uplink, tunnel cannot be established.

## Scenario Runbooks

## Scenario 1: Another Internet/WiFi Network Is Available
Goal: bring gateway online quickly using available local infrastructure.

1. Power gateway, AP/switch.
2. Connect gateway uplink (`eth0`) to available router/switch (DHCP expected).
3. Join local control WiFi from laptop (`192.168.10.x`) **or** use uplink-side IP if known.
4. Access gateway:
   - SSH: `ssh recomputer@192.168.10.1` (preferred stable fallback)
   - or `ssh recomputer@<uplink-ip>`
5. Verify services:
   - `systemctl is-active bsm-network.service bsm-web.service`
   - `curl -I http://127.0.0.1:5001`
6. Verify web access from laptop:
   - `http://192.168.10.1:5001` (or uplink-side IP + `:5001`)
7. If using VPN/tunnel, confirm it comes up after uplink is active.

## Scenario 2: Field Site, Before Starlink Is Connected
Goal: keep local operations and manage headlessly with no WAN.

1. Power gateway + AP/switch.
2. Join local control WiFi (`192.168.10.x`) from laptop.
3. SSH to local gateway IP:
   - `ssh recomputer@192.168.10.1`
4. Confirm local services/UI:
   - `systemctl is-active bsm-network.service bsm-web.service`
   - open `http://192.168.10.1:5001`
5. Continue local testing/data collection as needed.
6. Note: remote VPN/tunnel access is unavailable until uplink exists.

## Scenario 3: Field Site, Starlink Available
Goal: maintain local operations and add WAN/cloud + remote support.

1. Start from Scenario 2 (local headless access working).
2. Connect Starlink LAN/Ethernet adapter to gateway uplink (`eth0`).
3. Verify WAN route and DNS from SSH shell.
4. Validate cloud path (`bsm_network` upload cycle / outbound reachability).
5. Verify `bsm_web` remains reachable locally at `192.168.10.1:5001`.
6. Bring up VPN/tunnel (Tailscale/WireGuard) and confirm remote access.
7. Remember: Starlink CGNAT typically blocks direct inbound port forwarding, so VPN/tunnel is preferred.

## Quick Verification Commands (SSH)
```bash
systemctl is-active bsm-network.service bsm-web.service
systemctl is-active bsm-healthcheck.timer
systemctl status --no-pager bsm-network.service bsm-web.service | sed -n '1,40p'
systemctl status --no-pager bsm-healthcheck.timer bsm-healthcheck.service | sed -n '1,40p'
ss -ltnp | rg ':5001'
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:5001
ip a
ip route
```

## Interface Mode Check (SSH)
Use these commands to confirm `eth0` is DHCP and `eth1` is static:

```bash
ip -4 addr show eth0
ip -4 addr show eth1
```

Expected pattern:
- `eth0` shows an address like `192.168.1.x/24` with `scope global dynamic` (DHCP uplink).
- `eth1` shows `192.168.10.1/24` with `valid_lft forever` (static local/control LAN).

Current verified example (2026-05-22):
- `eth0`: `192.168.1.6/24`, `dynamic`
- `eth1`: `192.168.10.1/24`, `valid_lft forever`

## Failure Recovery (No Monitor)
1. Try local WiFi/LAN access to `192.168.10.1`.
2. If unavailable, use direct Ethernet fallback and static laptop IP (`192.168.10.50/24`).
3. Check services/logs:
   - `journalctl -u bsm-web.service -u bsm-network.service -n 100 --no-pager`
4. Restart services if needed:
   - `sudo systemctl restart bsm-web.service bsm-network.service`
5. Check automatic recovery activity:
   - `journalctl -t bsm-healthcheck -n 50 --no-pager`

## Notes
- Keep local static management path (`192.168.10.1`) as your primary recovery method.
- Keep VPN/tunnel as remote convenience, not the only control path.
- Maintain current service definitions in repo (`deploy/systemd/`) and setup guide (`docs/deploy/SYSTEMD_GATEWAY_SETUP.md`).
