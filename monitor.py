#!/usr/bin/env python3
"""
Lantern Power Monitor - 32-bit Pi Zero W client.

Replaces the Java Pi service for ARMv6 hardware. Reads CT sensors via two
MCP3008 ADCs on the LPMPCB1 board, calculates real power, and posts readings
to the Lantern Tomcat server in the same BSON+gzip format as the Java client.

Setup:
  1. Flash 32-bit Raspberry Pi OS Lite on the Pi Zero W.
  2. Copy all files to /opt/powermonitor/ on the Pi.
  3. Install dependencies (see README).
  4. Enable the systemd service.
  5. Use the Lantern app to discover and configure the hub via Bluetooth,
     OR manually edit /opt/powermonitor/config.json with host and auth_code.

Dependencies:
  apt:  python3-dbus python3-gi bluetooth
  pip:  spidev pymongo requests pycryptodome netifaces
"""

import gzip
import json
import logging
import math
import os
import signal
import struct
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import bson
import requests

from mcp3008 import ADCReader
from sampler import calibrate_voltage, sample_breakers
from bluetooth_service import HubBleService

WORKING_DIR = '/opt/powermonitor/'
CONFIG_PATH = os.path.join(WORKING_DIR, 'config.json')
CACHE_DIR   = os.path.join(WORKING_DIR, 'cache/')
LOG_PATH    = os.path.join(WORKING_DIR, 'log.txt')
VERSION     = '1.0.0-lighter'

os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH),
    ]
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        log.error(f"Failed to save config: {e}")


# ---------------------------------------------------------------------------
# Server communication
# ---------------------------------------------------------------------------

def make_session(cfg):
    s = requests.Session()
    s.headers.update({'auth_code': cfg.get('auth_code', '')})
    return s


def to_zip_bson(doc):
    """Encode a dict as BSON then gzip-compress it (matches Java ZipUtils.zip = GZIP BEST_SPEED)."""
    return gzip.compress(bson.encode(doc), compresslevel=1)


def from_zip_bson(data):
    """Decompress and decode a BSON payload."""
    return bson.decode(gzip.decompress(data))


def post(session, host, path, payload):
    """POST a zip-bson payload. Returns response bytes or None on failure."""
    try:
        url = host.rstrip('/') + '/' + path
        resp = session.post(url, data=payload,
                            headers={'Content-Type': 'application/octet-stream'},
                            timeout=10)
        if resp.status_code == 200:
            return resp.content
        log.warning(f"POST /{path} returned HTTP {resp.status_code}")
    except Exception as e:
        log.error(f"POST /{path} failed: {e}")
    return None


def fetch_config(session, host):
    """GET /config from the server. Returns parsed JSON dict or None."""
    try:
        url = host.rstrip('/') + '/config'
        resp = session.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        log.error(f"GET /config returned HTTP {resp.status_code}")
    except Exception as e:
        log.error(f"GET /config failed: {e}")
    return None


def post_config(session, host, server_config):
    """POST updated BreakerConfig (with new calibration factor) back to /config."""
    try:
        payload = to_zip_bson(server_config)
        url = host.rstrip('/') + '/config'
        resp = session.post(url, data=payload,
                            headers={'Content-Type': 'application/octet-stream'},
                            timeout=10)
        if resp.status_code == 200:
            log.info("Calibration config posted to server")
            return True
        log.warning(f"POST /config returned HTTP {resp.status_code}")
    except Exception as e:
        log.error(f"POST /config failed: {e}")
    return False


# ---------------------------------------------------------------------------
# Server config parsing
# ---------------------------------------------------------------------------

def find_hub(server_config, hub_index):
    """Return the BreakerHub dict for our hub index, or None."""
    for h in server_config.get('breaker_hubs') or []:
        if h.get('hub') == hub_index:
            return h
    return None


def collect_breakers(groups):
    """Recursively flatten breakers from all groups and sub-groups."""
    result = []
    for g in groups or []:
        result.extend(g.get('breakers') or [])
        result.extend(collect_breakers(g.get('sub_groups')))
    return result


def breakers_for_hub(server_config, hub_index):
    """Return all Breaker dicts assigned to hub_index with valid ports (1-15)."""
    all_breakers = collect_breakers(server_config.get('breaker_groups') or [])
    return [b for b in all_breakers
            if b.get('hub') == hub_index and 1 <= b.get('port', 0) <= 15]


def build_breaker_list(server_breakers, hub):
    """Convert server breaker dicts into the format sampler.py expects."""
    port_cal = float(hub.get('port_calibration_factor') or 1.0)
    result = []
    for b in server_breakers:
        result.append({
            'port':            b['port'],
            'panel':           b.get('panel', 1),
            'space':           b.get('space', 0),
            'port_cal':        port_cal,
            'breaker_cal':     float(b.get('calibration_factor') or 1.0),
            'low_pass_filter': float(b.get('low_pass_filter') or 0.0),
            'polarity':        b.get('polarity') or 'NORMAL',
            'double_power':    bool(b.get('double_power', False)),
        })
    return result


# ---------------------------------------------------------------------------
# BSON payload builders
# ---------------------------------------------------------------------------

def build_batch_payload(hub_index, account_id, readings, read_time_ms):
    """Build the power/batch BSON document matching Java PowerPoster."""
    reading_docs = []
    for r in readings:
        panel = r['panel']
        space = r['space']
        reading_docs.append({
            '_id':         f"{account_id}-{panel}-{space}",
            'account_id':  account_id,
            'panel':       panel,
            'space':       space,
            'key':         f"{panel}-{space}",
            'read_time':   bson.Int64(read_time_ms),
            'hub_version': VERSION,
            'power':       r['power'],
            'voltage':     r['voltage'],
        })
    return to_zip_bson({'hub': hub_index, 'readings': reading_docs})


def build_minute_payload(hub_index, account_id, minute, avg_voltage,
                         breaker_readings_by_key):
    """
    Build the power/hub BSON document (HubPowerMinute).
    breaker_readings_by_key: dict of (panel, space) -> list of float watts (up to 60)
    """
    breaker_docs = []
    for (panel, space), watts_list in breaker_readings_by_key.items():
        # Pack as big-endian 32-bit floats in a 240-byte buffer (60 slots)
        # Matches Java BreakerPowerMinuteSerializer which uses ByteBuffer (big-endian default)
        buf = bytearray(240)
        for i, w in enumerate(watts_list[:60]):
            struct.pack_into('>f', buf, i * 4, float(w))
        breaker_docs.append({
            'panel':    panel,
            'space':    space,
            'readings': bson.Binary(bytes(buf)),
        })

    doc = {
        '_id':        f"{account_id}-{hub_index}-{minute}",
        'account_id': account_id,
        'hub':        hub_index,
        'minute':     minute,
        'voltage':    float(avg_voltage),
        'breakers':   breaker_docs,
    }
    return to_zip_bson(doc)


# ---------------------------------------------------------------------------
# Minute post cache — mirrors Java PowerPoster which writes .min files on failure
# ---------------------------------------------------------------------------

def cache_minute_payload(payload: bytes):
    """Write a failed minute post to the cache directory to retry later."""
    try:
        path = os.path.join(CACHE_DIR, f"{uuid.uuid4()}.min")
        with open(path, 'wb') as f:
            f.write(payload)
        log.info(f"Minute payload cached to {path}")
    except Exception as e:
        log.error(f"Failed to cache minute payload: {e}")


def flush_cache(session, host):
    """Retry any cached minute payloads. Stops on first failure."""
    try:
        for fname in sorted(os.listdir(CACHE_DIR)):
            if not fname.endswith('.min'):
                continue
            fpath = os.path.join(CACHE_DIR, fname)
            try:
                with open(fpath, 'rb') as f:
                    payload = f.read()
                if post(session, host, 'power/hub', payload) is not None:
                    os.remove(fpath)
                    log.info(f"Flushed cached minute: {fname}")
                else:
                    break  # Stop on first failure, try again next minute
            except Exception as e:
                log.error(f"Error flushing cache file {fname}: {e}")
    except Exception as e:
        log.error(f"Error reading cache directory: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info(f"Lantern Power Monitor (32-bit Python {VERSION}) starting")

    cfg = load_config()

    # ---------------------------------------------------------------------------
    # Start BLE first — this is how the user configures the hub on first run.
    # The app discovers the hub via BLE, then writes host/auth_code into config.json.
    # ---------------------------------------------------------------------------
    ble = HubBleService(
        config=cfg,
        config_path=CONFIG_PATH,
        version=VERSION,
        hub_name=f"Lantern Hub {cfg.get('hub', 0)}",
        on_reload=lambda: subprocess.run(['systemctl', 'restart', 'powermonitor']),
        log_path=LOG_PATH,
    )
    ble.start()
    log.info("BLE hub service started — waiting for app to connect if needed")

    # Wait for host and auth_code to be set (either already in config or written by BLE)
    wait_logged = False
    while not (cfg.get('host') and cfg.get('auth_code')):
        if not wait_logged:
            log.warning("host and auth_code not set. Use the Lantern app to configure via Bluetooth.")
            wait_logged = True
        time.sleep(5)
        cfg.update(load_config())  # Reload in case BLE wrote new values

    host      = cfg['host'].rstrip('/')
    hub_index = cfg.get('hub', 0)
    debug     = cfg.get('debug', False)

    session = make_session(cfg)

    # Fetch breaker config from server
    log.info("Fetching breaker config from server...")
    server_config = None
    for attempt in range(5):
        server_config = fetch_config(session, host)
        if server_config:
            break
        log.warning(f"Config fetch attempt {attempt + 1}/5 failed, retrying in 5s...")
        time.sleep(5)

    if not server_config:
        log.error("Could not load breaker config from server. Exiting.")
        sys.exit(1)

    # _id comes back from JSON as a string ("123") because BreakerConfigSerializer
    # writes it as String.valueOf(accountId). Coerce to int for BSON encoding.
    account_id = int(server_config.get('_id') or 0)
    ble.set_account_id(account_id)

    hub = find_hub(server_config, hub_index)
    while not hub:
        log.warning(f"No hub with index {hub_index} in server config — waiting for app to configure hub...")
        time.sleep(30)
        server_config = fetch_config(session, host)
        if server_config:
            account_id = int(server_config.get('_id') or 0)
            ble.set_account_id(account_id)
            hub = find_hub(server_config, hub_index)

    voltage_cal = float(hub.get('voltage_calibration_factor') or 1.0)
    frequency   = int(hub.get('frequency') or 60)
    server_breakers = breakers_for_hub(server_config, hub_index)

    while not server_breakers:
        log.warning(f"No breakers configured for hub {hub_index} — waiting for app to configure breakers...")
        time.sleep(30)
        server_config = fetch_config(session, host)
        if server_config:
            hub = find_hub(server_config, hub_index)
            server_breakers = breakers_for_hub(server_config, hub_index)

    log.info(f"Hub {hub_index}: {len(server_breakers)} breakers, "
             f"voltage_cal={voltage_cal:.6f}, {frequency}Hz")

    adc = ADCReader()

    # Graceful shutdown on SIGTERM/SIGINT
    def shutdown(sig, frame):
        log.info("Shutting down...")
        adc.close()
        ble.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Voltage calibration
    needs_calibration = cfg.get('needs_calibration', True)
    if needs_calibration:
        log.info("Running voltage calibration...")
        new_cal, detected_freq = calibrate_voltage(adc, voltage_cal, frequency)
        if new_cal is not None:
            voltage_cal = new_cal
            frequency   = detected_freq
            hub['voltage_calibration_factor'] = voltage_cal
            hub['frequency'] = frequency
            post_config(session, host, server_config)
            cfg['needs_calibration'] = False
            save_config(cfg)
        else:
            log.warning("Calibration failed — continuing with existing factor")

    breakers = build_breaker_list(server_breakers, hub)
    log.info(f"Monitoring ports: {[b['port'] for b in breakers]}")

    # Per-minute tracking
    minute_readings = {}   # (panel, space) -> [None|float] * 60, indexed by second-in-minute
    minute_voltages = []
    last_minute     = int(time.time() / 60)

    log.info("Monitoring loop started")

    while True:
        # Record start of this iteration so we can align to ~1s intervals
        iter_start = time.monotonic()

        read_time    = datetime.now(timezone.utc)
        read_time_ms = int(read_time.timestamp() * 1000)

        results = sample_breakers(adc, breakers, voltage_cal, interval_s=1.0)

        if debug:
            for r in results:
                log.info(f"  Panel{r['panel']} Space{r['space']:>2}: "
                         f"{r['power']:8.2f}W  {r['voltage']:.2f}V  "
                         f"({r['samples']} samples)")

        if not results:
            continue

        # Accumulate per-minute data
        cur_second = int(read_time.timestamp()) % 60
        cur_minute = int(read_time.timestamp() / 60)

        for r in results:
            key = (r['panel'], r['space'])
            if key not in minute_readings:
                minute_readings[key] = [None] * 60
            minute_readings[key][cur_second] = r['power']

        # Voltage: all breakers share the same transformer, just take the first non-zero
        v = next((r['voltage'] for r in results if r['voltage'] > 0), 0.0)
        if v > 0:
            minute_voltages.append(v)

        # Minute rollover — build and send HubPowerMinute
        if cur_minute != last_minute:
            avg_v = sum(minute_voltages) / len(minute_voltages) if minute_voltages else 0.0
            minute_payload = build_minute_payload(
                hub_index, account_id, last_minute, avg_v,
                {k: [x for x in vs if x is not None]
                 for k, vs in minute_readings.items()}
            )
            # Flush any cached payloads first, then send current one
            flush_cache(session, host)
            if post(session, host, 'power/hub', minute_payload) is None:
                log.warning("Failed to post minute summary — caching for retry")
                cache_minute_payload(minute_payload)

            minute_readings = {}
            minute_voltages = []
            last_minute     = cur_minute

        # POST per-second batch
        batch_payload = build_batch_payload(hub_index, account_id, results, read_time_ms)
        resp = post(session, host, 'power/batch', batch_payload)

        # Handle server commands returned in the batch response (e.g. ReloadConfig)
        if resp and len(resp) > 0:
            try:
                cmds = from_zip_bson(resp)
                for cmd in cmds.get('commands') or []:
                    char = cmd.get('characteristic')
                    data = cmd.get('data') or b''
                    log.info(f"Server command: {char}")
                    ble._write(char, bytes(data))
            except Exception:
                pass

        # The sampling loop already consumes ~1s, so this sleep only covers
        # any remaining time if posting was very fast.
        elapsed = time.monotonic() - iter_start
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)
