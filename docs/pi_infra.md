# Pi Infrastructure

## Purpose
Document Raspberry Pi / gateway infrastructure setup, operations, and recovery steps.

## System Overview
- Hostname: `reComputer-R100x`
- OS/version: `TBD (capture with: uname -a / cat /etc/os-release)`
- Role (gateway/controller/etc.): Gateway/controller for Arduino data collection and cloud relay
- Primary interfaces:
  - `eth0`: Home/WAN-side uplink (`192.168.1.6/24` in home lab)
  - `eth1`: Local field/control LAN gateway (`192.168.10.1/24`)
  - `wlan0`: Local WiFi-side interface (`192.168.10.135/24` observed in lab)

## Network Topology
- Local control LAN: `192.168.10.0/24`
- WAN uplink: `eth0` to upstream router/Starlink (DHCP on upstream network)
- AP/switch layout: AP/switch infrastructure on `eth1` local network
- Static IP assignments:
  - Gateway `eth1`: `192.168.10.1`
  - Gateway `eth0` (home lab): `192.168.1.6` (may vary by DHCP/environment)

## Services
- `bsm_network`:
  - Service name: `bsm-network.service`
  - Start command: `python3 bsm_network.py ...` (profile-driven config)
  - Logs: `journalctl -u bsm-network.service`
- `bsm_web`:
  - Service name: `bsm-web.service`
  - Start command: `python3 bsm_web.py`
  - Logs: `journalctl -u bsm-web.service`
- Systemd source of truth: `docs/deploy/SYSTEMD_GATEWAY_SETUP.md`

## Boot / Auto-Start
- `systemd` units: enabled and verified after reboot
- Restart policy: `Restart=always` (see systemd setup doc)
- Watchdog policy: systemd restart policy active; hardware watchdog planned later
- Setup/reference: `docs/deploy/SYSTEMD_GATEWAY_SETUP.md`

## Remote Access
- SSH: Enabled and used for headless administration
- VPN/tunnel (Tailscale/WireGuard/etc.): Planned (recommended: Tailscale first)
- Access URLs/ports:
  - `bsm_web` bind: `0.0.0.0`
  - `bsm_web` port: `5001`
  - Home-lab access example: `http://192.168.1.6:5001`
  - Field-lan access example: `http://192.168.10.1:5001`

## Security
- User accounts: `recomputer` (additional accounts TBD)
- Key auth status: `TBD`
- Firewall rules summary: `TBD`

## Backups
- Config files to back up:
  - `config/network_profile.json`
  - `docs/network_map.dual_mode.json`
  - `data/` operational metadata needed for continuity
  - `docs/deploy/SYSTEMD_GATEWAY_SETUP.md` and tracked unit templates
- Backup method/schedule: `TBD`

## Recovery Checklist
1. Verify network interfaces and IP addresses.
2. Verify `bsm_network` and `bsm_web` service status.
3. Verify local web access (`bsm_web` port).
4. Verify WAN/cloud connectivity.
5. If no uplink available, direct-connect laptop to local LAN and access `http://192.168.10.1:5001`.

## Change Log
- 2026-05-21: Initialized `docs/pi_infra.md` scaffold.
- 2026-05-21: Prefilled with current known interface, IP, and `bsm_web` access details.
- 2026-05-21: Consolidated systemd details to `docs/deploy/SYSTEMD_GATEWAY_SETUP.md`.
