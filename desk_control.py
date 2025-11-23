#!/usr/bin/env python3
"""
Core desk control library for UPLIFT/Jiecang standing desks.

Protocol adapted from https://github.com/librick/uplift-ble
Supports multiple desk variants with proper packet formatting and wake sequence.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, List
from bleak import BleakScanner, BleakClient
from bleak.uuids import normalize_uuid_16

CACHE_FILE = Path.home() / ".desk_address"
CONFIG_CACHE_FILE = Path.home() / ".desk_config"
LOG_FILE = Path.home() / ".desk_log"

# Desk identifier - last 6 hex digits of MAC address
# Appears in BLE device name as "BLE Device 99B319"
DESK_ID = "99B319"


@dataclass
class DeskConfig:
    """Configuration for a specific desk variant."""
    variant_name: str
    service_uuid: str
    input_char_uuid: str  # Write commands here
    output_char_uuid: str  # Read notifications here
    requires_wake: bool = True


# Supported desk variants - different Jiecang hardware revisions use different UUIDs
DESK_CONFIGS: Dict[str, DeskConfig] = {
    normalize_uuid_16(0xFF00): DeskConfig(
        variant_name="JIECANG_0xFF00",
        service_uuid=normalize_uuid_16(0xFF00),
        input_char_uuid=normalize_uuid_16(0xFF01),
        output_char_uuid=normalize_uuid_16(0xFF02),
    ),
    normalize_uuid_16(0xFE60): DeskConfig(
        variant_name="JIECANG_0xFE60",
        service_uuid=normalize_uuid_16(0xFE60),
        input_char_uuid=normalize_uuid_16(0xFE61),
        output_char_uuid=normalize_uuid_16(0xFE62),
    ),
    normalize_uuid_16(0x00FF): DeskConfig(
        variant_name="JIECANG_0x00FF",
        service_uuid=normalize_uuid_16(0x00FF),
        input_char_uuid=normalize_uuid_16(0x01FF),
        output_char_uuid=normalize_uuid_16(0x02FF),
    ),
    normalize_uuid_16(0xFF12): DeskConfig(
        variant_name="JIECANG_0xFF12",
        service_uuid=normalize_uuid_16(0xFF12),
        input_char_uuid=normalize_uuid_16(0xFF01),
        output_char_uuid=normalize_uuid_16(0xFF02),
    ),
}

DESK_SERVICE_UUIDS = list(DESK_CONFIGS.keys())


def create_command_packet(opcode: int, payload: bytes = b"") -> bytes:
    """
    Create a command packet for the Uplift BLE protocol.

    Frame format: [0xF1, 0xF1] [opcode] [length] [payload...] [checksum] [0x7E]
    Checksum = (opcode + length + sum(payload)) mod 256
    """
    payload_len = len(payload)
    checksum = (opcode + payload_len + sum(payload)) & 0xFF
    return bytes([0xF1, 0xF1, opcode, payload_len, *payload, checksum, 0x7E])


# Command opcodes from the protocol
class DeskOpcode:
    WAKE = 0x00
    MOVE_UP = 0x01
    MOVE_DOWN = 0x02
    PRESET_1 = 0x05  # Sit
    PRESET_2 = 0x06  # Stand
    PRESET_3 = 0x07
    PRESET_4 = 0x08
    MOVE_TO_HEIGHT = 0x1B
    STOP = 0x2B


# Pre-built command packets
COMMANDS = {
    'wake': create_command_packet(DeskOpcode.WAKE),
    'sit': create_command_packet(DeskOpcode.PRESET_1),
    'stand': create_command_packet(DeskOpcode.PRESET_2),
    'up': create_command_packet(DeskOpcode.MOVE_UP),
    'down': create_command_packet(DeskOpcode.MOVE_DOWN),
    'stop': create_command_packet(DeskOpcode.STOP),
}

def get_cached_config() -> Optional[DeskConfig]:
    """Load cached desk configuration."""
    if CONFIG_CACHE_FILE.exists():
        try:
            variant_name = CONFIG_CACHE_FILE.read_text().strip()
            for config in DESK_CONFIGS.values():
                if config.variant_name == variant_name:
                    return config
        except Exception:
            pass
    return None


def cache_config(config: DeskConfig):
    """Save desk configuration to cache."""
    CONFIG_CACHE_FILE.write_text(config.variant_name)


async def detect_desk_config(client: BleakClient) -> Optional[DeskConfig]:
    """Detect which desk variant we're connected to by checking available services."""
    try:
        services = client.services
        for service in services:
            service_uuid = service.uuid.lower()
            if service_uuid in DESK_CONFIGS:
                return DESK_CONFIGS[service_uuid]
    except Exception:
        pass
    return None


async def get_desk_address() -> Optional[str]:
    """Get cached address or scan for desk."""
    # Try cached address first
    if CACHE_FILE.exists():
        cached_addr = CACHE_FILE.read_text().strip()
        if cached_addr:
            return cached_addr

    # Scan for desk by name or by advertising known service UUIDs
    found_device = [None]

    def detection_callback(device, adv_data):
        # Match by name
        if device.name and DESK_ID in device.name.upper():
            found_device[0] = device
            return
        # Match by advertised service UUIDs
        if adv_data.service_uuids:
            for uuid in adv_data.service_uuids:
                if uuid.lower() in DESK_CONFIGS:
                    found_device[0] = device
                    return

    scanner = BleakScanner(detection_callback)
    await scanner.start()

    for _ in range(100):  # 10 seconds max
        await asyncio.sleep(0.1)
        if found_device[0]:
            break

    await scanner.stop()

    if found_device[0]:
        CACHE_FILE.write_text(found_device[0].address)
        return found_device[0].address

    return None


def log_position(preset_name: str):
    """Log desk position change with timestamp."""
    timestamp = datetime.now().isoformat()
    log_entry = f"{timestamp},{preset_name}\n"
    with open(LOG_FILE, 'a') as f:
        f.write(log_entry)


async def send_wake_sequence(client: BleakClient, input_uuid: str, count: int = 3):
    """
    Send wake commands to prepare the desk for receiving actual commands.

    The desk BLE adapter often needs to be "woken up" before it will respond
    to movement commands. This sends multiple wake packets with delays.
    """
    wake_cmd = COMMANDS['wake']
    for _ in range(count):
        try:
            await client.write_gatt_char(input_uuid, wake_cmd, response=False)
        except Exception:
            pass  # Wake commands may fail, that's okay
        await asyncio.sleep(0.1)


async def send_command(client: BleakClient, config: DeskConfig, command_name: str) -> bool:
    """
    Send a command to the desk with proper wake sequence.

    Returns True on success, False on failure.
    """
    if command_name not in COMMANDS:
        return False

    command = COMMANDS[command_name]
    input_uuid = config.input_char_uuid

    try:
        # Send wake sequence if required
        if config.requires_wake:
            await send_wake_sequence(client, input_uuid)

        # Send the actual command
        await client.write_gatt_char(input_uuid, command, response=False)

        # Brief delay then send again for reliability
        await asyncio.sleep(0.15)
        await client.write_gatt_char(input_uuid, command, response=False)

        return True
    except Exception as e:
        print(f"Error sending command: {e}")
        return False


async def move_to_preset(preset_name: str) -> int:
    """
    Move desk to preset position (sit/stand).

    Returns 0 on success, 1 on failure.
    """
    if preset_name not in COMMANDS:
        print(f"Unknown preset: {preset_name}")
        return 1

    addr = await get_desk_address()
    if not addr:
        print("Could not find desk")
        return 1

    try:
        async with BleakClient(addr, timeout=20.0) as client:
            # Try to use cached config first
            config = get_cached_config()

            # If no cache, detect from connected device
            if not config:
                config = await detect_desk_config(client)
                if config:
                    cache_config(config)
                    print(f"Detected desk variant: {config.variant_name}")

            # Fallback to FF00 variant (most common for UPLIFT)
            if not config:
                config = DESK_CONFIGS[normalize_uuid_16(0xFF00)]
                print(f"Using default variant: {config.variant_name}")

            success = await send_command(client, config, preset_name)

            if success:
                log_position(preset_name)
                return 0
            else:
                return 1

    except Exception as e:
        print(f"Connection error: {e}")
        # Clear cache on connection error - address may have changed
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
        return 1


async def get_desk_status() -> Optional[dict]:
    """
    Connect to desk and retrieve current status information.

    Returns dict with height and other info, or None on failure.
    """
    addr = await get_desk_address()
    if not addr:
        return None

    try:
        async with BleakClient(addr, timeout=10.0) as client:
            config = get_cached_config()
            if not config:
                config = await detect_desk_config(client)
            if not config:
                config = DESK_CONFIGS[normalize_uuid_16(0xFF00)]

            # TODO: Set up notification handler and request height
            # For now, just return that we connected successfully
            return {
                'connected': True,
                'address': addr,
                'variant': config.variant_name,
            }
    except Exception as e:
        return {'connected': False, 'error': str(e)}


def main(preset_name: str) -> int:
    """Main entry point."""
    return asyncio.run(move_to_preset(preset_name))


# CLI support
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        result = main(sys.argv[1])
        sys.exit(result)
    else:
        print("Usage: desk_control.py <sit|stand|up|down|stop>")
        sys.exit(1)
