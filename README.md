# Lighter — Lightweight Lantern Power Monitor for Pi Zero W

A lightweight Python client for the [Lantern Power Monitor](https://github.com/MarkBryanMilligan/LanternPowerMonitor) ecosystem, designed for **32-bit ARMv6** hardware where the official Java client can't run.

The official Java client requires a JVM, which needs ARMv7 or newer. Lighter runs on the Pi Zero W's BCM2835 (ARMv6) while remaining **fully wire-compatible** with the existing Lantern Tomcat server and Android/iOS app — same BSON+gzip wire format, same BLE GATT service, same calibration logic.

---

## Hardware Requirements

- Raspberry Pi Zero W (BCM2835, ARMv6)
- [LPMPCB1](https://github.com/MarkBryanMilligan/LanternPowerMonitor) 15-port power monitor PCB
- AC/AC voltage reference transformer (9V)
- CT current sensors (one per breaker)

---

## Software Requirements

**Raspberry Pi OS:** 32-bit (armhf) Lite — Bullseye or Bookworm

**System packages:**
```bash
sudo apt install -y python3-dbus python3-gi python3-pip bluetooth bluez
```

**Python packages:**
```bash
sudo pip3 install --break-system-packages pymongo pycryptodome netifaces
```
> `spidev` is pre-installed on Raspberry Pi OS. `requests` is included with the system Python.

---

## Setup

### 1. Enable SPI
```bash
sudo raspi-config nonint do_spi 0
```

### 2. Enable BlueZ experimental mode
Edit `/lib/systemd/system/bluetooth.service` and add `--experimental` to the `ExecStart` line:
```
ExecStart=/usr/lib/bluetooth/bluetoothd --experimental
```

Ensure Bluetooth comes up on boot (add to `/etc/rc.local`):
```bash
rfkill unblock bluetooth
hciconfig hci0 up
```

### 3. Deploy files
Copy all `.py` files and `powermonitor.service` to `/opt/powermonitor/` on the Pi:
```bash
sudo mkdir -p /opt/powermonitor/cache
sudo cp *.py /opt/powermonitor/
sudo cp config.json.example /opt/powermonitor/config.json
sudo cp powermonitor.service /etc/systemd/system/
```

### 4. Configure
Either edit `/opt/powermonitor/config.json` manually, or use the **Lantern app** to configure the hub via Bluetooth — the service will advertise as `Lantern Hub <n>` and wait for the app to write the host and auth code.

If self-hosting the server, set the host to your server URL:
```json
{
    "host": "http://your-server:8081/currentmonitor/",
    "auth_code": "",
    "hub": 0,
    "needs_calibration": true,
    "debug": false
}
```

> The app will overwrite `host` and `auth_code` when it configures the hub via BLE. After the app configures the hub, manually update `host` to point to your self-hosted server if needed.

### 5. Enable and start the service
```bash
sudo systemctl daemon-reload
sudo systemctl enable powermonitor
sudo systemctl start powermonitor
sudo journalctl -u powermonitor -f
```

---

## File Overview

| File | Description |
|------|-------------|
| `monitor.py` | Main entry point — BLE startup, server config fetch, sampling loop, data posting |
| `mcp3008.py` | SPI driver for dual MCP3008 ADCs on LPMPCB1 |
| `sampler.py` | Real power and Vrms calculation; voltage calibration |
| `bluetooth_service.py` | BlueZ D-Bus GATT server matching Java `BluetoothConfig` exactly |
| `requirements.txt` | pip dependencies |
| `powermonitor.service` | systemd unit file |
| `config.json.example` | Example configuration file |

---

## How It Works

1. On startup, the BLE service advertises as `Lantern Hub <n>` so the Lantern app can discover and configure it.
2. Once `host` and `auth_code` are set (via app or manually), the monitor fetches breaker configuration from the server.
3. Voltage is calibrated using the AC/AC transformer reference — detects 50/60 Hz automatically and scales to 120V/230V.
4. A round-robin sampling loop reads voltage + current for each breaker at ~2500 samples/second on the Pi Zero W.
5. Per-second readings are posted to `/power/batch`; per-minute summaries to `/power/hub`.
6. Failed minute posts are cached to `/opt/powermonitor/cache/` and retried on the next minute rollover.

---

## Self-Hosted Server

See the [LanternPowerMonitor](https://github.com/MarkBryanMilligan/LanternPowerMonitor) repo for server setup. The WAR must be deployed as `currentmonitor.war` (the app expects the `/currentmonitor/` context path). To point the app at your server, log out of the Lantern app and enter your server URL on the login screen before creating an account.

---

## Compatibility

Tested with:
- Raspberry Pi Zero W (BCM2835, ARMv6)
- Raspberry Pi OS Bookworm 32-bit (2025)
- LPMPCB1 (15-port, dual MCP3008)
- Lantern Power Monitor server v2.0.0
- Lantern Power Monitor Android app
