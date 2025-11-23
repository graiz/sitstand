#!/usr/bin/env python3
"""Core desk control library"""
import asyncio
import time
from datetime import datetime
from pathlib import Path
from bleak import BleakScanner, BleakClient

CACHE_FILE = Path.home() / ".desk_address"
LOG_FILE = Path.home() / ".desk_log"
CONTROL_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# Desk identifier - last 6 hex digits of MAC address (DD:C9:A9:99:B3:19)
# Appears in BLE device name as "BLE Device 99B319"
DESK_ID = "99B319"

# Preset configurations - memory slot commands
PRESETS = {
    'sit': bytes.fromhex("f1f10500057e"),    # Memory slot 1
    'stand': bytes.fromhex("f1f10600067e"),  # Memory slot 2
}

async def get_desk_address():
    """Get cached address or scan for desk"""
    # Try cached address first
    if CACHE_FILE.exists():
        cached_addr = CACHE_FILE.read_text().strip()
        if cached_addr:
            return cached_addr

    # Scan for desk
    found_addr = [None]

    def detection_callback(device, adv_data):
        if device.name and DESK_ID in device.name.upper():
            found_addr[0] = device.address

    scanner = BleakScanner(detection_callback)
    await scanner.start()

    for _ in range(20):
        await asyncio.sleep(0.1)
        if found_addr[0]:
            break

    await scanner.stop()

    if found_addr[0]:
        # Cache the address for next time
        CACHE_FILE.write_text(found_addr[0])
        return found_addr[0]

    return None

def log_position(preset_name):
    """Log desk position change with timestamp"""
    timestamp = datetime.now().isoformat()
    log_entry = f"{timestamp},{preset_name}\n"

    with open(LOG_FILE, 'a') as f:
        f.write(log_entry)

async def move_to_preset(preset_name):
    """Move desk to preset position (sit/stand)"""
    if preset_name not in PRESETS:
        return 1

    command = PRESETS[preset_name]

    addr = await get_desk_address()
    if not addr:
        return 1

    # Connect and send command twice with brief pause
    # First send wakes the desk, second actually executes
    async with BleakClient(addr, timeout=10.0) as client:
        # First send: wake up
        await client.write_gatt_char(CONTROL_UUID, command, response=False)
        await asyncio.sleep(0.2)

        # Second send: actual command (now that desk is awake)
        await client.write_gatt_char(CONTROL_UUID, command, response=False)

    # Log the position change
    log_position(preset_name)

    return 0

def main(preset_name):
    """Main entry point"""
    return asyncio.run(move_to_preset(preset_name))
