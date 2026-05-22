# Gateway systemd setup

## Files

### /etc/systemd/system/bsm-network.service

```ini
[Unit]
Description=BSM Network Process
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=recomputer
WorkingDirectory=/home/recomputer/Arduino_WIFI
ExecStart=/usr/bin/python3 -u /home/recomputer/Arduino_WIFI/bsm_network.py

Restart=always
RestartSec=3

Environment=PYTHONUNBUFFERED=1

TasksMax=100
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/bsm-web.service

```ini
[Unit]
Description=BSM Web Process
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=recomputer
WorkingDirectory=/home/recomputer/Arduino_WIFI
ExecStart=/usr/bin/python3 -u /home/recomputer/Arduino_WIFI/bsm_web.py

Restart=always
RestartSec=3

Environment=PYTHONUNBUFFERED=1

TasksMax=100
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/bsm-healthcheck.service

```ini
[Unit]
Description=BSM health check and auto-recovery
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/home/recomputer/Arduino_WIFI/scripts/bsm_healthcheck.sh
```

### /etc/systemd/system/bsm-healthcheck.timer

```ini
[Unit]
Description=Run BSM health check every minute

[Timer]
OnBootSec=45s
OnUnitActiveSec=60s
AccuracySec=5s
Unit=bsm-healthcheck.service
Persistent=true

[Install]
WantedBy=timers.target
```

## Install / apply

```bash
sudo cp bsm-network.service /etc/systemd/system/
sudo cp bsm-web.service /etc/systemd/system/
sudo cp bsm-healthcheck.service /etc/systemd/system/
sudo cp bsm-healthcheck.timer /etc/systemd/system/
sudo chmod +x /home/recomputer/Arduino_WIFI/scripts/bsm_healthcheck.sh

sudo systemctl daemon-reload
sudo systemctl enable bsm-network.service bsm-web.service bsm-healthcheck.timer
sudo systemctl restart bsm-network.service bsm-web.service
sudo systemctl restart bsm-healthcheck.timer
```

## Check status

```bash
systemctl status bsm-network.service bsm-web.service
journalctl -b -u bsm-network.service -u bsm-web.service
systemctl status bsm-healthcheck.timer bsm-healthcheck.service
```

## Follow logs live

```bash
journalctl -f -u bsm-network.service -u bsm-web.service
journalctl -f -t bsm-healthcheck -u bsm-healthcheck.service
```

## Quick recovery

Stop both services:

```bash
sudo systemctl stop bsm-network.service bsm-web.service
```

Kill stuck Python processes:

```bash
sudo pkill -f bsm_network.py
sudo pkill -f bsm_web.py
```

Restart cleanly:

```bash
sudo systemctl daemon-reload
sudo systemctl restart bsm-network.service bsm-web.service
sudo systemctl restart bsm-healthcheck.timer
```

Reboot if system is unstable:

```bash
sudo reboot
```

## Notes

Both services are long-running, so both use:

```ini
Type=simple
Restart=always
```

Do not use `Type=oneshot` for these services.

The health check service is intentionally `Type=oneshot` and is scheduled by the timer.
