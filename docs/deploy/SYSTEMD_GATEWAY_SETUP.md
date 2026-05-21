# Gateway systemd setup

## Files

### /etc/systemd/system/bsm-network.service

```ini
[Unit]
Description=BSM Network Process
After=network.target

[Service]
Type=simple
User=recomputer
WorkingDirectory=/home/recomputer/Arduino_WIFI
ExecStart=/usr/bin/python3 -u /home/recomputer/Arduino_WIFI/bsm_network.py

Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

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
After=network.target

[Service]
Type=simple
User=recomputer
WorkingDirectory=/home/recomputer/Arduino_WIFI
ExecStart=/usr/bin/python3 -u /home/recomputer/Arduino_WIFI/bsm_web.py

Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

Environment=PYTHONUNBUFFERED=1

TasksMax=100
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```

## Install / apply

```bash
sudo cp bsm-network.service /etc/systemd/system/
sudo cp bsm-web.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable bsm-network.service bsm-web.service
sudo systemctl restart bsm-network.service bsm-web.service
```

## Check status

```bash
systemctl status bsm-network.service bsm-web.service
journalctl -b -u bsm-network.service -u bsm-web.service
```

## Follow logs live

```bash
journalctl -f -u bsm-network.service -u bsm-web.service
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