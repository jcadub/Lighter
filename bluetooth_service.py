#!/usr/bin/env python3
"""
BLE GATT service for Lantern Hub configuration.

Implements the same GATT service and characteristics as the Java BluetoothConfig,
so the Lantern Power Monitor app can discover, connect to, and configure this hub
exactly as it would with the original Java client.

Requires (apt): python3-dbus, python3-gi, bluetooth
Requires (pip): pycryptodome, netifaces
"""

import gzip
import logging
import os
import struct
import subprocess
import threading

import bson
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
import netifaces
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from gi.repository import GLib

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# D-Bus / BlueZ interface names
# ---------------------------------------------------------------------------
BLUEZ_SERVICE        = 'org.bluez'
GATT_MANAGER_IFACE   = 'org.bluez.GattManager1'
LE_ADV_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
LE_ADV_IFACE         = 'org.bluez.LEAdvertisement1'
GATT_SERVICE_IFACE   = 'org.bluez.GattService1'
GATT_CHRC_IFACE      = 'org.bluez.GattCharacteristic1'
DBUS_OM_IFACE        = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE      = 'org.freedesktop.DBus.Properties'

BASE_PATH = '/com/lanternsoftware'

# ---------------------------------------------------------------------------
# UUIDs — matches Java UUIDFormatter("c5650001-d50f-49af-b906-cada0dc17937")
# format(idx) replaces chars 4-8 with zero-padded hex idx
# ---------------------------------------------------------------------------
def _uuid(idx):
    return f'c565{idx:04x}-d50f-49af-b906-cada0dc17937'

SERVICE_UUID = _uuid(1)

CHARS = {
    # name: (idx, flags)
    'WifiCredentials': (_uuid(2),  ['write']),
    'AuthCode':        (_uuid(3),  ['write']),
    'HubIndex':        (_uuid(4),  ['read', 'write']),
    'Restart':         (_uuid(5),  ['write']),
    'Reboot':          (_uuid(6),  ['write']),
    'AccountId':       (_uuid(7),  ['read']),
    'NetworkState':    (_uuid(8),  ['read']),
    'Flash':           (_uuid(9),  ['write']),
    'Host':            (_uuid(10), ['write']),
    'Log':             (_uuid(11), ['read']),
    'NetworkDetails':  (_uuid(12), ['read']),
    'Shutdown':        (_uuid(13), ['write']),
    'Version':         (_uuid(14), ['read']),
    'Update':          (_uuid(15), ['write']),
    'ReloadConfig':    (_uuid(16), ['write']),
    'BoardVersion':    (_uuid(17), ['read']),
}

# ---------------------------------------------------------------------------
# WiFi credential decryption
# AES key from Java: new AESTool(37320708309265127L, -8068168662055796771L,
#                                -4867793276337148572L, 4425609941731230765L)
# Longs are packed big-endian into a 32-byte AES-256 key.
# Decrypt format: [16B IV][ciphertext] -> AES-CBC -> [16B salt][gzip+BSON]
# ---------------------------------------------------------------------------
_WIFI_AES_KEY = struct.pack('>qqqq',
    37320708309265127,
    -8068168662055796771,
    -4867793276337148572,
    4425609941731230765,
)

def decrypt_wifi_creds(payload: bytes):
    """Return (ssid, password) or (None, None) on failure."""
    try:
        iv         = payload[:16]
        ciphertext = payload[16:]
        cipher     = AES.new(_WIFI_AES_KEY, AES.MODE_CBC, iv)
        plaintext  = unpad(cipher.decrypt(ciphertext), AES.block_size)[16:]  # strip 16B salt
        doc = bson.decode(gzip.decompress(plaintext))
        return doc.get('ssid'), doc.get('pwd')
    except Exception as e:
        log.error(f"Failed to decrypt WiFi credentials: {e}")
        return None, None

# ---------------------------------------------------------------------------
# Network helpers — matches Java NetworkAdapter (ETHERNET=0x01, WIFI=0x02)
# ---------------------------------------------------------------------------
def get_network_state_mask() -> int:
    mask = 0
    try:
        for iface in netifaces.interfaces():
            ips = [a['addr'] for a in netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                   if not a['addr'].startswith('127.')]
            if ips:
                if iface.startswith(('eth', 'en')):
                    mask |= 0x01  # ETHERNET
                elif iface.startswith(('wlan', 'wl')):
                    mask |= 0x02  # WIFI
    except Exception:
        pass
    return mask

def get_network_ips():
    """Return (wifi_ips, ethernet_ips) lists."""
    wifi_ips, eth_ips = [], []
    try:
        for iface in netifaces.interfaces():
            ips = [a['addr'] for a in netifaces.ifaddresses(iface).get(netifaces.AF_INET, [])
                   if not a['addr'].startswith('127.')]
            if ips:
                if iface.startswith(('eth', 'en')):
                    eth_ips.extend(ips)
                elif iface.startswith(('wlan', 'wl')):
                    wifi_ips.extend(ips)
    except Exception:
        pass
    return wifi_ips, eth_ips

# ---------------------------------------------------------------------------
# D-Bus base class for objects that expose properties
# ---------------------------------------------------------------------------
class DBusObject(dbus.service.Object):
    def __init__(self, bus, path):
        super().__init__(bus, path)
        self._path = path

    def get_path(self):
        return dbus.ObjectPath(self._path)

# ---------------------------------------------------------------------------
# BLE Advertisement
# ---------------------------------------------------------------------------
class LanternAdvertisement(DBusObject):
    PATH = BASE_PATH + '/advertisement0'

    def __init__(self, bus, hub_name):
        super().__init__(bus, self.PATH)
        self._hub_name = hub_name

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        return self._get_properties()[prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self._get_properties()

    def _get_properties(self):
        return {
            'Type':           dbus.String('peripheral'),
            'ServiceUUIDs':   dbus.Array([SERVICE_UUID], signature='s'),
            'LocalName':      dbus.String(self._hub_name),
            'IncludeTxPower': dbus.Boolean(True),
        }

    @dbus.service.method(LE_ADV_IFACE, in_signature='', out_signature='')
    def Release(self):
        log.info("BLE advertisement released")

# ---------------------------------------------------------------------------
# GATT Characteristic
# ---------------------------------------------------------------------------
class LanternCharacteristic(DBusObject):
    def __init__(self, bus, idx, name, uuid, flags, service_path, read_cb, write_cb):
        path = service_path + '/char' + str(idx)
        super().__init__(bus, path)
        self._name      = name
        self._uuid      = uuid
        self._flags     = flags
        self._svc_path  = service_path
        self._read_cb   = read_cb
        self._write_cb  = write_cb

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        return self._get_properties()[prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self._get_properties()

    def _get_properties(self):
        return {
            'Service': dbus.ObjectPath(self._svc_path),
            'UUID':    dbus.String(self._uuid),
            'Flags':   dbus.Array(self._flags, signature='s'),
        }

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        if self._read_cb:
            result = self._read_cb(self._name)
            if result is not None:
                offset = int(options.get('offset', 0))
                return dbus.Array(result[offset:], signature='y')
        return dbus.Array([], signature='y')

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        if self._write_cb:
            self._write_cb(self._name, bytes(value))

# ---------------------------------------------------------------------------
# GATT Service
# ---------------------------------------------------------------------------
class LanternService(DBusObject):
    PATH = BASE_PATH + '/service0'

    def __init__(self, bus, read_cb, write_cb):
        super().__init__(bus, self.PATH)
        self._characteristics = []
        for idx, (name, (uuid, flags)) in enumerate(CHARS.items()):
            self._characteristics.append(
                LanternCharacteristic(bus, idx, name, uuid, flags, self.PATH, read_cb, write_cb)
            )

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        return self._get_properties()[prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self._get_properties()

    def _get_properties(self):
        return {
            'UUID':    dbus.String(SERVICE_UUID),
            'Primary': dbus.Boolean(True),
            'Characteristics': dbus.Array(
                [c.get_path() for c in self._characteristics],
                signature='o'
            ),
        }

    def get_characteristics(self):
        return self._characteristics

# ---------------------------------------------------------------------------
# GATT Application — top-level ObjectManager
# ---------------------------------------------------------------------------
class LanternApplication(DBusObject):
    def __init__(self, bus, hub_name, read_cb, write_cb):
        super().__init__(bus, BASE_PATH)
        self._service = LanternService(bus, read_cb, write_cb)
        self._adv     = LanternAdvertisement(bus, hub_name)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        objects = {}
        svc = self._service
        objects[svc.get_path()] = {
            GATT_SERVICE_IFACE: svc.GetAll(GATT_SERVICE_IFACE)
        }
        for ch in svc.get_characteristics():
            objects[ch.get_path()] = {
                GATT_CHRC_IFACE: ch.GetAll(GATT_CHRC_IFACE)
            }
        return objects

    def get_advertisement(self):
        return self._adv

# ---------------------------------------------------------------------------
# Hub BLE service — ties app state to GATT callbacks
# ---------------------------------------------------------------------------
class HubBleService:
    """
    Manages the BLE GATT service. Call start() to begin advertising.
    The monitor.py main loop interacts via the on_write callback.
    """

    def __init__(self, config, config_path, version, hub_name='Lantern Hub',
                 on_reload=None, log_path=None):
        """
        Args:
            config:      dict — live config (modified in-place on writes)
            config_path: path to config.json to persist changes
            version:     version string for the Version characteristic
            hub_name:    BLE device name shown in the app
            on_reload:   callback() invoked when app sends ReloadConfig
            log_path:    path to log file for Log characteristic
        """
        self._config      = config
        self._config_path = config_path
        self._version     = version
        self._hub_name    = hub_name
        self._on_reload   = on_reload
        self._log_path    = log_path or '/opt/powermonitor/log.txt'
        self._account_id  = 0
        self._mainloop    = None
        self._thread      = None

    def set_account_id(self, account_id: int):
        self._account_id = account_id

    # -- Characteristic READ handler -----------------------------------------

    def _read(self, name: str):
        try:
            if name == 'HubIndex':
                return bytes([self._config.get('hub', 0) & 0xFF])

            if name == 'AccountId':
                return struct.pack('>i', self._account_id)

            if name == 'NetworkState':
                return bytes([get_network_state_mask()])

            if name == 'NetworkDetails':
                wifi_ips, eth_ips = get_network_ips()
                mask = get_network_state_mask()
                status = {
                    'wifi_ips':       wifi_ips,
                    'ethernet_ips':   eth_ips,
                    'ping_successful': bool(mask),
                }
                return gzip.compress(bson.encode(status), compresslevel=1)

            if name == 'Version':
                return self._version.encode('utf-8')

            if name == 'BoardVersion':
                return bytes([0])  # LPMPCB1 = version 0

            if name == 'Log':
                try:
                    with open(self._log_path) as f:
                        lines = f.readlines()
                    tail = ''.join(lines[-15:]) if len(lines) > 15 else ''.join(lines)
                    # Java uses ZipUtils.zip which is GZIP, not raw zlib
                    return gzip.compress(tail.encode('utf-8'), compresslevel=1)
                except Exception:
                    return b''

        except Exception as e:
            log.error(f"BLE read error for {name}: {e}")
        return None

    # -- Characteristic WRITE handler ----------------------------------------

    def _write(self, name: str, value: bytes):
        try:
            log.info(f"BLE write: {name} ({len(value)} bytes)")

            if name == 'Host':
                host = value.decode('utf-8').strip()
                if host:
                    self._config['host'] = host
                    self._save_config()
                    log.info(f"Host set to: {host}")

            elif name == 'AuthCode':
                code = value.decode('utf-8').strip()
                if code:
                    self._config['auth_code'] = code
                    self._save_config()
                    log.info("AuthCode updated")

            elif name == 'HubIndex':
                if value:
                    self._config['hub'] = int(value[0])
                    self._save_config()
                    log.info(f"Hub index set to: {self._config['hub']}")

            elif name == 'WifiCredentials':
                ssid, pwd = decrypt_wifi_creds(bytes(value))
                if ssid and pwd:
                    log.info(f"Connecting to WiFi SSID: {ssid}")
                    subprocess.Popen(['nmcli', 'd', 'wifi', 'connect', ssid, 'password', pwd])
                    subprocess.Popen(['history', '-c'])

            elif name == 'ReloadConfig':
                log.info("ReloadConfig received from app")
                if self._on_reload:
                    threading.Thread(target=self._on_reload, daemon=True).start()

            elif name == 'Restart':
                log.info("Restart requested via BLE")
                subprocess.Popen(['systemctl', 'restart', 'powermonitor'])

            elif name == 'Reboot':
                log.info("Reboot requested via BLE")
                subprocess.Popen(['reboot', 'now'])

            elif name == 'Shutdown':
                log.info("Shutdown requested via BLE")
                subprocess.Popen(['shutdown', 'now'])

            elif name == 'Flash':
                # LED flash control — Pi Zero W activity LED is /sys/class/leds/led0
                if not value or value[0] == 0:
                    _set_led(False)
                else:
                    threading.Thread(target=_flash_led, daemon=True).start()

        except Exception as e:
            log.error(f"BLE write error for {name}: {e}")

    def _save_config(self):
        try:
            import json
            with open(self._config_path, 'w') as f:
                json.dump(self._config, f, indent=4)
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    # -- Start / stop --------------------------------------------------------

    def start(self):
        """Start the BLE GATT service in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name='BLE')
        self._thread.start()

    def stop(self):
        if self._mainloop:
            self._mainloop.quit()

    def _run(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        try:
            adapter = _find_adapter(bus)
            if not adapter:
                log.error("No Bluetooth adapter found — BLE service unavailable")
                return

            # Set the adapter alias so the app sees our hub name in the device list
            _set_adapter_alias(bus, adapter, self._hub_name)

            app = LanternApplication(bus, self._hub_name, self._read, self._write)
            adv = app.get_advertisement()

            gatt_manager = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, adapter),
                GATT_MANAGER_IFACE
            )
            adv_manager = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, adapter),
                LE_ADV_MANAGER_IFACE
            )

            gatt_manager.RegisterApplication(
                app.get_path(), {},
                reply_handler=lambda: log.info("GATT application registered"),
                error_handler=lambda e: log.error(f"GATT register failed: {e}")
            )
            adv_manager.RegisterAdvertisement(
                adv.get_path(), {},
                reply_handler=lambda: log.info(f"BLE advertising as '{self._hub_name}'"),
                error_handler=lambda e: log.error(f"Advertising register failed: {e}")
            )

            self._mainloop = GLib.MainLoop()
            self._mainloop.run()

        except Exception as e:
            log.error(f"BLE service error: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_adapter_alias(bus, adapter_path, alias):
    """Set the Bluetooth adapter's alias (device name shown during BLE scan)."""
    try:
        adapter_props = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE, adapter_path),
            DBUS_PROP_IFACE
        )
        adapter_props.Set('org.bluez.Adapter1', 'Alias', dbus.String(alias))
        log.info(f"Bluetooth adapter alias set to '{alias}'")
    except Exception as e:
        log.warning(f"Could not set adapter alias: {e}")


def _find_adapter(bus):
    """Return the D-Bus path of the first available Bluetooth adapter."""
    try:
        om = dbus.Interface(bus.get_object(BLUEZ_SERVICE, '/'), DBUS_OM_IFACE)
        for path, interfaces in om.GetManagedObjects().items():
            if GATT_MANAGER_IFACE in interfaces:
                return path
    except Exception as e:
        log.error(f"Error finding BT adapter: {e}")
    return None


def _set_led(on: bool):
    try:
        trigger = 'default-on' if on else 'none'
        with open('/sys/class/leds/led0/trigger', 'w') as f:
            f.write(trigger)
    except Exception:
        pass


def _flash_led():
    import time
    for _ in range(10):
        _set_led(True)
        time.sleep(0.2)
        _set_led(False)
        time.sleep(0.2)
