#!/usr/bin/env python3
"""Control ROSESELSA earbuds.

Usage:
  rosebuds [status] [--json]
  rosebuds battery [--json]
  rosebuds dump
  rosebuds anc [on|wind|off|trans]   (ANC mode)
  rosebuds anc-level [light|moderate|deep]
  rosebuds eq [classic|pop|hifi|rock|custom]
  rosebuds game [on|off]
  rosebuds dual [on|off]   (dual-device connection, restarts the earbuds)
  rosebuds codec [aac|ldac|lhdc]   (aac also means SBC, restarts the earbuds)

Leave out the value of a setting to show the current one.
Add -v to print raw bytes to stderr.
The earbuds must already be connected to the Linux host.
It finds them by their ROSELINK control service. Set ROSEBUDS_ADDR to select a specific pair.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass

# Serial service the ROSELINK app uses. It's in the ROSELINK app code, so it marks
# the product platform rather than a single pair. BlueZ lists it without connecting.
CONTROL_UUID = "0cf12d31-fac3-4553-bd80-d6832e7b3931"
# Bluetooth names the earbuds report themselves. Renaming them on the computer
# changes the alias, not this.
TESTED_MODELS = {"ROSE Ceramics U"}
ATT_PSM = 31
# Vendor GATT service 00fe: write to 00f1, replies arrive as notifications on 00f2.
WRITE_UUID = 0x00F1
NOTIFY_UUID = 0x00F2
MODE_KEY = 0x09
GAME_KEY = 0x0E
# Left, right, case. Low 7 bits = percent, high bit = charging, ff = unknown.
BATTERY_KEY = 0x0C
MODES = {"on": 1, "wind": 4, "off": 2, "trans": 3}
LEVEL_KEY = 0x2C
LEVELS = {"light": 1, "moderate": 3, "deep": 5}
EQ_KEY = 0x2A
# "custom" is the app's Customized EQ slot. The command that sends the curve is not yet decoded.
EQ_PRESETS = {"classic": 3, "pop": 1, "hifi": 0, "rock": 2, "custom": 4}
DUAL_KEY = 0x32
# "aac" is the app's AAC/SBC option. Dual-device connection only works with it.
CODEC_KEY = 0x2B
CODECS = {"aac": 0, "ldac": 1, "lhdc": 2}
# Changing these restarts the earbuds, so the new value can't be read back right away.
RESTART_KEYS = {DUAL_KEY, CODEC_KEY}
VERBOSE = False
# Exact key list ROSELINK asks for when it opens.
APP_QUERY_KEYS = bytes.fromhex(
    "01 07 08 09 0c 0d 0e 12 2a 2b 2c 2d 2e 2f 31 32 33 36 37 38 39 3a 3b 3c 3d 3f 45 46 49"
)
ACK_KEY = 0xFE


class EarbudsError(Exception):
    pass


@dataclass
class Device:
    addr: str
    name: str
    alias: str
    connected: bool

    @property
    def tested(self):
        return self.name in TESTED_MODELS


def _bluetoothctl(*args):
    try:
        return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        raise EarbudsError(f"Can't run bluetoothctl ({e}). Is BlueZ installed?") from e


def _device_info(addr):
    """Return (Device, list of service UUIDs) as BlueZ knows them."""
    fields, uuids = {}, []
    for line in _bluetoothctl("info", addr).splitlines():
        key, _, value = line.strip().partition(": ")
        if key == "UUID":
            uuids.append(value.rsplit("(", 1)[-1].rstrip(")").lower())
        elif value:
            fields.setdefault(key, value)
    device = Device(addr, fields.get("Name", ""), fields.get("Alias", addr), fields.get("Connected") == "yes")
    return device, uuids


def find_earbuds():
    """Pick the earbuds to talk to: ROSEBUDS_ADDR if set, else a connected (or paired) pair with the control service."""
    addr = os.environ.get("ROSEBUDS_ADDR")
    if addr:
        return _device_info(addr.upper())[0]
    found = []
    for line in _bluetoothctl("devices").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            device, uuids = _device_info(parts[1])
            if CONTROL_UUID in uuids:
                found.append(device)
    if not found:
        raise EarbudsError("No paired ROSESELSA earbuds found. Pair them first, or set ROSEBUDS_ADDR.")
    return max(found, key=lambda d: d.connected)


def frame(seq, tlvs):
    body = bytes([0xFF, seq]) + b"".join(bytes([len(v) + 1, k]) + v for k, v in tlvs)
    return body + bytes([sum(body) & 0xFF, 0xAA])


def parse_frames(buf):
    """Split earbud data into frames: dd seq (len key value)... checksum aa.

    There is no frame length, so a frame ends where a valid checksum + aa follows a TLV.
    """
    frames, i = [], 0
    while i < len(buf):
        if buf[i] != 0xDD:
            i += 1
            continue
        pos, tlvs = i + 2, []
        while pos < len(buf):
            ln = buf[pos]
            tlvs.append((buf[pos + 1], bytes(buf[pos + 2:pos + 1 + ln])))
            pos += 1 + ln
            if pos + 1 < len(buf) and buf[pos + 1] == 0xAA and sum(buf[i:pos]) & 0xFF == buf[pos]:
                frames.append(tlvs)
                pos += 2
                break
        i = pos
    return frames


def battery_levels(settings):
    """Return [(name, percent or None, charging)] for left, right and case."""
    raw = settings.get(BATTERY_KEY, b"")
    return [(name, None if b == 0xFF else b & 0x7F, b != 0xFF and bool(b & 0x80))
            for name, b in zip(("left", "right", "case"), raw)]


class Earbuds:
    """Talks ATT directly over an L2CAP socket.

    BlueZ doesn't open the ATT link over classic Bluetooth itself, and the earbuds
    only sometimes do, so the D-Bus GATT API can't be relied on.
    """

    def __init__(self, device=None):
        self.device = device or find_earbuds()
        if not self.device.connected:
            raise EarbudsError(f"{self.device.alias} isn't connected.")
        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        self.sock.settimeout(3)
        try:
            self.sock.connect((self.device.addr, ATT_PSM))
        except OSError as e:
            self.sock.close()
            raise EarbudsError(f"Can't reach {self.device.alias} ({e.strerror or e}).") from e
        self.seq = 0
        self.notifications = []
        handles = self._find_value_handles()
        if WRITE_UUID not in handles or NOTIFY_UUID not in handles:
            self.sock.close()
            raise EarbudsError("Earbuds' control service not found.")
        self.write_handle = handles[WRITE_UUID]
        self.notify_handle = handles[NOTIFY_UUID]
        # The notification config descriptor directly follows the value handle.
        self._att_request(struct.pack("<BHH", 0x12, self.notify_handle + 1, 0x0001))

    def _recv(self):
        pdu = self.sock.recv(1024)
        if pdu[0] == 0x1B:
            self.notifications.append(pdu)
        elif pdu[0] == 0x1D:
            self.sock.send(b"\x1e")  # confirm indications, or the earbuds stop sending
        return pdu

    def _att_request(self, pdu):
        self.sock.send(pdu)
        while True:
            resp = self._recv()
            if resp[0] not in (0x1B, 0x1D):
                return resp

    def _find_value_handles(self):
        """Map 16-bit characteristic UUIDs to their value handles."""
        handles, start = {}, 0x0001
        while start <= 0xFFFF:
            resp = self._att_request(struct.pack("<BHHH", 0x08, start, 0xFFFF, 0x2803))
            if resp[0] != 0x09:
                break
            size = resp[1]
            for i in range(2, len(resp), size):
                decl, _props, value = struct.unpack("<HBH", resp[i:i + 5])
                if size == 7:
                    handles[struct.unpack("<H", resp[i + 5:i + 7])[0]] = value
                start = decl + 1
        return handles

    def request(self, tlvs, done_key, wait_s=1.5):
        """Send a frame and collect replies until one contains done_key, or wait_s passes."""
        out = frame(self.seq, tlvs)
        if VERBOSE:
            print("tx:", out.hex(" "), file=sys.stderr)
        self.seq = (self.seq + 1) & 0xFF
        self.notifications.clear()
        self.sock.send(struct.pack("<BH", 0x52, self.write_handle) + out)
        self.sock.settimeout(wait_s)
        frames = []
        try:
            while not any(k == done_key for tlvs in frames for k, _ in tlvs):
                self._recv()
                rx = b"".join(n[3:] for n in self.notifications
                              if struct.unpack("<H", n[1:3])[0] == self.notify_handle)
                frames = parse_frames(rx)
        except socket.timeout:
            pass
        if VERBOSE:
            print("rx:", frames or "(nothing)", file=sys.stderr)
        return frames

    def get_all(self):
        # A late echo of an earlier change carries only that key, so wait for key 01,
        # which only the full status reply contains.
        settings = {}
        for tlvs in self.request([(0xFA, APP_QUERY_KEYS)], done_key=APP_QUERY_KEYS[0]):
            settings.update((k, v) for k, v in tlvs if k != ACK_KEY)
        return settings

    def set(self, key, value, wait_s=2.0):
        """Change a setting and return all settings once the change shows up.

        The earbuds acknowledge right away but apply the change ~0.5 s later,
        so a status read straight after the ack still shows the old value.
        """
        self.request([(key, bytes([value]))], done_key=ACK_KEY)
        deadline = time.monotonic() + wait_s
        while True:
            settings = self.get_all()
            if settings.get(key) == bytes([value]) or time.monotonic() > deadline:
                return settings
            time.sleep(0.2)

    def set_mode(self, value):
        return self.set(MODE_KEY, value)

    def set_game(self, on):
        return self.set(GAME_KEY, int(on))

    def set_level(self, value):
        return self.set(LEVEL_KEY, value)

    def set_eq(self, value):
        return self.set(EQ_KEY, value)

    def set_restart(self, key, value):
        """Change a setting that restarts the earbuds. Return True if they took it.

        The new value can't be read back here, so an ack or a dropped link counts as taken.
        """
        try:
            frames = self.request([(key, bytes([value]))], done_key=ACK_KEY)
        except ConnectionResetError:
            return True
        return any(k == ACK_KEY for tlvs in frames for k, _ in tlvs)

    def close(self):
        self.sock.close()


ON_OFF = {"on": 1, "off": 0}
# Status label and value names for each setting, in status order.
SETTING_NAMES = {
    MODE_KEY: ("anc mode", MODES),
    LEVEL_KEY: ("anc level", LEVELS),
    EQ_KEY: ("eq", EQ_PRESETS),
    GAME_KEY: ("game mode", ON_OFF),
    DUAL_KEY: ("dual device", ON_OFF),
    CODEC_KEY: ("codec", CODECS),
}
COMMANDS = {"anc": MODE_KEY, "anc-level": LEVEL_KEY, "eq": EQ_KEY, "game": GAME_KEY,
            "dual": DUAL_KEY, "codec": CODEC_KEY}
VALUE_ALIASES = {"transparency": "trans", "sbc": "aac"}


def usage_error(message):
    sys.exit(f"{message}\nRun 'rosebuds --help' for usage.")


def parse_command(args):
    """Return (action, key, value), or exit with an error.

    Actions: status, battery, dump, show (key), set (key, value).
    """
    if not args:
        return "status", None, None
    cmd, rest = args[0], args[1:]
    if cmd in ("status", "battery", "dump"):
        if rest:
            usage_error(f"'{cmd}' takes no arguments.")
        return cmd, None, None
    if cmd in COMMANDS:
        key = COMMANDS[cmd]
        if not rest:
            return "show", key, None
        if len(rest) > 1:
            usage_error(f"'{cmd}' takes one value.")
        values = SETTING_NAMES[key][1]
        name = VALUE_ALIASES.get(rest[0], rest[0])
        if name not in values:
            usage_error(f"Unknown {cmd} value '{rest[0]}'. Choose from: {', '.join(values)}.")
        return "set", key, values[name]
    if cmd.startswith("-"):
        usage_error(f"Unknown option '{cmd}'.")
    usage_error(f"Unknown command '{cmd}'.")


def setting_text(settings, key):
    values = SETTING_NAMES[key][1]
    raw = settings.get(key)
    if not raw:
        return "unknown"
    names = {v: k for k, v in values.items()}
    return names.get(raw[0], f"unknown ({raw.hex(' ')})")


def battery_text(settings):
    parts = [f"{name} unknown" if pct is None else f"{name} {pct}%" + (" (charging)" if charging else "")
             for name, pct, charging in battery_levels(settings)]
    return ", ".join(parts) or "unknown"


def battery_json(settings):
    return {name: {"percent": pct, "charging": charging} for name, pct, charging in battery_levels(settings)}


def status_json(device, settings):
    data = {"device": {"name": device.alias, "address": device.addr}}
    for key, (label, values) in SETTING_NAMES.items():
        names = {v: k for k, v in values.items()}
        raw = settings.get(key)
        data[label.replace(" ", "_")] = names.get(raw[0]) if raw else None
    data["battery"] = battery_json(settings)
    return data


def print_status(device, settings):
    rows = [("device", device.alias)]
    rows += [(label, setting_text(settings, key)) for key, (label, _) in SETTING_NAMES.items()]
    rows.append(("battery", battery_text(settings)))
    for label, text in rows:
        print(f"{label + ':':<15}{text}")


def switch_restart(buds, key, value):
    label, values = SETTING_NAMES[key]
    name = {v: k for k, v in values.items()}[value]
    command = {k: c for c, k in COMMANDS.items()}[key]
    settings = buds.get_all()
    if settings.get(key) == bytes([value]):
        print(f"{label}: already {name}")
        return
    if key == CODEC_KEY and value != CODECS["aac"] and settings.get(DUAL_KEY) == b"\x01":
        sys.exit("Only AAC and SBC codecs work in this mode. Turn it off first: rosebuds dual off")
    if not buds.set_restart(key, value):
        sys.exit(f"The earbuds didn't reply. Run 'rosebuds {command}' in a few seconds to check.")
    print(f"{label}: {name}. The earbuds are restarting to apply it.")
    if key == DUAL_KEY and value:
        print("Only AAC and SBC codecs work in this mode.")


def main():
    global VERBOSE
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    VERBOSE = "-v" in args
    as_json = "--json" in args
    args = [a.lower() for a in args if a not in ("-v", "--json")]
    action, key, value = parse_command(args)
    if as_json and action not in ("status", "battery"):
        usage_error("--json works only with status and battery.")
    try:
        buds = Earbuds()
    except EarbudsError as e:
        sys.exit(str(e))
    if not buds.device.tested:
        print(f"Warning: {buds.device.name or buds.device.addr} hasn't been tested with this tool. "
              "Settings may not match what you pick.", file=sys.stderr)
    try:
        if key in RESTART_KEYS and action == "set":
            switch_restart(buds, key, value)
            return
        if value is None:
            settings = buds.get_all()
        else:
            settings = buds.set(key, value)
            if settings.get(key) != bytes([value]):
                sys.exit("The earbuds didn't apply the change.")
        if as_json:
            data = battery_json(settings) if action == "battery" else status_json(buds.device, settings)
            print(json.dumps(data, indent=2))
        elif action == "battery":
            for name, pct, charging in battery_levels(settings):
                if pct is None:
                    print(f"{name}: unknown")
                else:
                    print(f"{name}: {pct}%" + (" (charging)" if charging else ""))
        elif action == "dump":
            for k, v in sorted(settings.items()):
                print(f"{k:02x}: {v.hex(' ')}")
        elif action == "show":
            print(f"{SETTING_NAMES[key][0]}: {setting_text(settings, key)}")
        else:
            # After a change too: some changes affect other settings, e.g. setting the ANC level turns ANC on.
            print_status(buds.device, settings)
    except OSError as e:
        sys.exit(f"Connection lost ({e.strerror or e}). The earbuds may be restarting.")
    finally:
        buds.close()


if __name__ == "__main__":
    main()
