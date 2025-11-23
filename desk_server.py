#!/usr/bin/env python3
"""
Uplift Desk Web Controller
A simple web interface to control your sit/stand desk
"""

import asyncio
from aiohttp import web
import json
from bleak import BleakScanner, BleakClient
from typing import Optional

# MAC address from QR code
MAC_ADDRESS = "DD:C9:A9:99:B3:19"

# Characteristic UUIDs for Uplift desk
CONTROL_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# Linak protocol commands
MOVE_UP = bytes.fromhex("f1f10100017e")
MOVE_DOWN = bytes.fromhex("f1f10200027e")
STOP = bytes.fromhex("f1f12b002b7e")

# Memory slot commands (instant positioning!)
MEMORY_1 = bytes.fromhex("f1f10500057e")  # Memory slot 1
MEMORY_2 = bytes.fromhex("f1f10600067e")  # Memory slot 2 (Stand = 50959)
MEMORY_3 = bytes.fromhex("f1f10700077e")  # Memory slot 3
MEMORY_4 = bytes.fromhex("f1f10800087e")  # Memory slot 4

# Preset heights
PRESET_SIT = 11791    # 30.2 inches
PRESET_STAND = 50959  # 45.5 inches

# Global state
desk_client: Optional[BleakClient] = None
current_height = 0
connected = False
target_height = None
moving_to_preset = False

async def discover_desk_by_mac(mac_address: str, timeout: float = 10.0) -> Optional[str]:
    """Discover desk by MAC address from QR code"""
    mac_clean = mac_address.replace(":", "").upper()
    last_6_digits = mac_clean[-6:]

    print(f"Looking for device with '{last_6_digits}' in name...")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)

    for address, (device, adv_data) in devices.items():
        if device.name and last_6_digits in device.name.upper():
            print(f"Found: {device.name} at {device.address}")
            return device.address

    return None

def notification_handler(sender, data):
    """Handle height notifications from desk"""
    global current_height, moving_to_preset, target_height
    if len(data) >= 7:
        height_bytes = data[5:7]
        current_height = int.from_bytes(height_bytes, byteorder='big')
        print(f"Height: {current_height}")

        # Check if we've reached target height
        if moving_to_preset and target_height is not None:
            if abs(current_height - target_height) < 100:  # Within tolerance
                moving_to_preset = False
                target_height = None
                print(f"✓ Reached target height")

async def connect_to_desk():
    """Connect to the desk"""
    global desk_client, connected

    device_address = await discover_desk_by_mac(MAC_ADDRESS)
    if not device_address:
        raise Exception("Desk not found. Make sure it's powered on and press a button to wake it.")

    desk_client = BleakClient(device_address, timeout=20.0)
    await desk_client.connect()

    if desk_client.is_connected:
        await desk_client.start_notify(NOTIFY_UUID, notification_handler)
        connected = True
        print(f"✓ Connected to desk at {device_address}")
    else:
        raise Exception("Failed to connect to desk")

async def disconnect_from_desk():
    """Disconnect from desk"""
    global desk_client, connected
    if desk_client and desk_client.is_connected:
        await desk_client.stop_notify(NOTIFY_UUID)
        await desk_client.disconnect()
        connected = False
        print("✓ Disconnected from desk")

async def move_desk(direction: str, duration: float = 0.5):
    """Move desk up or down"""
    if not desk_client or not desk_client.is_connected:
        raise Exception("Not connected to desk")

    command = MOVE_UP if direction == "up" else MOVE_DOWN

    # Send move command repeatedly for duration
    end_time = asyncio.get_event_loop().time() + duration
    while asyncio.get_event_loop().time() < end_time:
        await desk_client.write_gatt_char(CONTROL_UUID, command, response=False)
        await asyncio.sleep(0.2)

    # Send stop command
    await desk_client.write_gatt_char(CONTROL_UUID, STOP, response=False)

async def move_to_height(target: int, timeout: float = 30.0):
    """Move desk to a specific height with improved control"""
    global moving_to_preset, target_height

    if not desk_client or not desk_client.is_connected:
        raise Exception("Not connected to desk")

    target_height = target
    moving_to_preset = True

    print(f"Moving to target height: {target} (current: {current_height})")

    start_time = asyncio.get_event_loop().time()
    last_height = current_height
    stall_count = 0

    # Larger tolerance to prevent oscillation
    TOLERANCE = 200  # Increased from 100
    SLOW_ZONE = 1000  # When to start slowing down
    STALL_THRESHOLD = 50  # Minimum movement per second

    while moving_to_preset:
        if asyncio.get_event_loop().time() - start_time > timeout:
            moving_to_preset = False
            await desk_client.write_gatt_char(CONTROL_UUID, STOP, response=False)
            raise Exception("Timeout reaching target height")

        diff = target - current_height

        # Check if we're close enough
        if abs(diff) < TOLERANCE:
            print(f"Within tolerance ({abs(diff)} < {TOLERANCE}), stopping")
            moving_to_preset = False
            break

        # Check for stall (desk not moving)
        if abs(current_height - last_height) < STALL_THRESHOLD:
            stall_count += 1
            if stall_count > 5:  # 1 second of no movement
                print(f"Desk appears stalled at {current_height}")
                moving_to_preset = False
                break
        else:
            stall_count = 0

        last_height = current_height

        # Move towards target
        if diff > 0:
            # Need to move up
            await desk_client.write_gatt_char(CONTROL_UUID, MOVE_UP, response=False)
        else:
            # Need to move down
            await desk_client.write_gatt_char(CONTROL_UUID, MOVE_DOWN, response=False)

        # When getting close to target, use shorter pulses
        if abs(diff) < SLOW_ZONE:
            await asyncio.sleep(0.1)  # Shorter pulses when close
            await desk_client.write_gatt_char(CONTROL_UUID, STOP, response=False)
            await asyncio.sleep(0.3)  # Wait to see effect
        else:
            await asyncio.sleep(0.2)  # Normal pulse rate

    # Final stop
    await desk_client.write_gatt_char(CONTROL_UUID, STOP, response=False)
    await asyncio.sleep(0.1)
    await desk_client.write_gatt_char(CONTROL_UUID, STOP, response=False)  # Double stop for safety

    print(f"✓ Final height: {current_height} (target was {target}, diff: {target - current_height})")

# Web handlers
async def handle_index(request):
    """Serve the main HTML page"""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Uplift Desk Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }

        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 500px;
            width: 100%;
        }

        h1 {
            text-align: center;
            color: #333;
            margin-bottom: 10px;
            font-size: 28px;
        }

        .status {
            text-align: center;
            padding: 15px;
            border-radius: 10px;
            margin: 20px 0;
            font-weight: 600;
        }

        .status.connected {
            background: #d4edda;
            color: #155724;
        }

        .status.disconnected {
            background: #f8d7da;
            color: #721c24;
        }

        .height-display {
            text-align: center;
            font-size: 48px;
            font-weight: bold;
            color: #667eea;
            margin: 30px 0;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 15px;
        }

        .height-label {
            font-size: 14px;
            color: #666;
            margin-bottom: 10px;
        }

        .controls {
            display: grid;
            gap: 15px;
            margin: 30px 0;
        }

        button {
            padding: 20px;
            font-size: 18px;
            font-weight: 600;
            border: none;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.3s ease;
            color: white;
        }

        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }

        button:active {
            transform: translateY(0);
        }

        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
        }

        .btn-up {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }

        .btn-down {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }

        .btn-stop {
            background: linear-gradient(135deg, #fa709a 0%, #fee140 100%);
        }

        .btn-connect {
            background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
        }

        .btn-disconnect {
            background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%);
        }

        .btn-sit {
            background: linear-gradient(135deg, #30cfd0 0%, #330867 100%);
        }

        .btn-stand {
            background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%);
        }

        .presets {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 15px;
        }

        .info {
            text-align: center;
            color: #666;
            font-size: 14px;
            margin-top: 20px;
        }

        .loading {
            display: none;
            text-align: center;
            color: #667eea;
            margin: 20px 0;
        }

        .loading.active {
            display: block;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🪑 Uplift Desk Controller</h1>

        <div id="status" class="status disconnected">
            Disconnected
        </div>

        <div class="height-display">
            <div class="height-label">Current Height</div>
            <div id="height">--</div>
        </div>

        <div class="loading" id="loading">
            ⏳ Working...
        </div>

        <div class="controls">
            <button id="connectBtn" class="btn-connect" onclick="connect()">
                Connect to Desk
            </button>

            <div class="presets">
                <button id="sitBtn" class="btn-sit" onclick="goToSit()" disabled>
                    🪑 Sit (30.2")
                </button>
                <button id="standBtn" class="btn-stand" onclick="goToStand()" disabled>
                    🧍 Stand (45.5")
                </button>
            </div>

            <button id="upBtn" class="btn-up" onclick="moveUp()" disabled>
                ⬆️ Move Up
            </button>

            <button id="downBtn" class="btn-down" onclick="moveDown()" disabled>
                ⬇️ Move Down
            </button>

            <button id="stopBtn" class="btn-stop" onclick="stop()" disabled>
                ⏹️ Stop
            </button>

            <button id="disconnectBtn" class="btn-disconnect" onclick="disconnect()" disabled>
                Disconnect
            </button>
        </div>

        <div class="info">
            MAC: DD:C9:A9:99:B3:19
        </div>
    </div>

    <script>
        let connected = false;

        async function updateStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();

                connected = data.connected;
                document.getElementById('status').className =
                    `status ${connected ? 'connected' : 'disconnected'}`;
                document.getElementById('status').textContent =
                    connected ? '✓ Connected' : 'Disconnected';

                document.getElementById('height').textContent =
                    connected && data.height ? data.height : '--';

                // Enable/disable buttons
                document.getElementById('connectBtn').disabled = connected;
                document.getElementById('sitBtn').disabled = !connected;
                document.getElementById('standBtn').disabled = !connected;
                document.getElementById('upBtn').disabled = !connected;
                document.getElementById('downBtn').disabled = !connected;
                document.getElementById('stopBtn').disabled = !connected;
                document.getElementById('disconnectBtn').disabled = !connected;
            } catch (e) {
                console.error('Error updating status:', e);
            }
        }

        function showLoading(show) {
            document.getElementById('loading').className =
                show ? 'loading active' : 'loading';
        }

        async function connect() {
            showLoading(true);
            try {
                const response = await fetch('/api/connect', { method: 'POST' });
                const data = await response.json();
                if (!data.success) {
                    alert('Connection failed: ' + data.error);
                }
            } catch (e) {
                alert('Error: ' + e.message);
            }
            showLoading(false);
            updateStatus();
        }

        async function disconnect() {
            showLoading(true);
            try {
                await fetch('/api/disconnect', { method: 'POST' });
            } catch (e) {
                console.error('Error disconnecting:', e);
            }
            showLoading(false);
            updateStatus();
        }

        async function moveUp() {
            try {
                await fetch('/api/move', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ direction: 'up' })
                });
            } catch (e) {
                console.error('Error moving up:', e);
            }
        }

        async function moveDown() {
            try {
                await fetch('/api/move', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ direction: 'down' })
                });
            } catch (e) {
                console.error('Error moving down:', e);
            }
        }

        async function stop() {
            try {
                await fetch('/api/stop', { method: 'POST' });
            } catch (e) {
                console.error('Error stopping:', e);
            }
        }

        async function goToSit() {
            showLoading(true);
            try {
                const response = await fetch('/api/preset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ preset: 'sit' })
                });
                const data = await response.json();
                if (!data.success) {
                    alert('Failed to move to sit position: ' + data.error);
                }
            } catch (e) {
                alert('Error: ' + e.message);
            }
            showLoading(false);
        }

        async function goToStand() {
            showLoading(true);
            try {
                const response = await fetch('/api/preset', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ preset: 'stand' })
                });
                const data = await response.json();
                if (!data.success) {
                    alert('Failed to move to stand position: ' + data.error);
                }
            } catch (e) {
                alert('Error: ' + e.message);
            }
            showLoading(false);
        }

        // Update status every second
        setInterval(updateStatus, 1000);
        updateStatus();
    </script>
</body>
</html>
"""
    return web.Response(text=html, content_type='text/html')

async def handle_status(request):
    """Return current status"""
    return web.json_response({
        'connected': connected,
        'height': current_height if connected else None
    })

async def handle_connect(request):
    """Connect to desk"""
    try:
        await connect_to_desk()
        return web.json_response({'success': True})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})

async def handle_disconnect(request):
    """Disconnect from desk"""
    await disconnect_from_desk()
    return web.json_response({'success': True})

async def handle_move(request):
    """Move desk up or down"""
    try:
        data = await request.json()
        direction = data.get('direction', 'up')
        await move_desk(direction)
        return web.json_response({'success': True})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})

async def handle_stop(request):
    """Stop desk movement"""
    global moving_to_preset
    try:
        moving_to_preset = False
        if desk_client and desk_client.is_connected:
            await desk_client.write_gatt_char(CONTROL_UUID, STOP, response=False)
        return web.json_response({'success': True})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})

async def handle_preset(request):
    """Move to preset height using memory slots"""
    try:
        data = await request.json()
        preset = data.get('preset')

        if not desk_client or not desk_client.is_connected:
            return web.json_response({'success': False, 'error': 'Not connected'})

        if preset == 'sit':
            # Try Memory 1 for instant sit positioning
            print(f"Activating Memory 1 (Sit position)")
            await desk_client.write_gatt_char(CONTROL_UUID, MEMORY_1, response=False)
        elif preset == 'stand':
            # Use Memory 2 for instant stand positioning!
            print(f"Activating Memory 2 (Stand position)")
            await desk_client.write_gatt_char(CONTROL_UUID, MEMORY_2, response=False)
        else:
            return web.json_response({'success': False, 'error': 'Invalid preset'})

        return web.json_response({'success': True})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})

async def on_shutdown(app):
    """Cleanup on shutdown"""
    await disconnect_from_desk()

def main():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/status', handle_status)
    app.router.add_post('/api/connect', handle_connect)
    app.router.add_post('/api/disconnect', handle_disconnect)
    app.router.add_post('/api/move', handle_move)
    app.router.add_post('/api/stop', handle_stop)
    app.router.add_post('/api/preset', handle_preset)
    app.on_shutdown.append(on_shutdown)

    print("="*70)
    print("UPLIFT DESK WEB CONTROLLER")
    print("="*70)
    print(f"\n🌐 Server starting at http://localhost:8080")
    print(f"📱 Open this URL in your browser to control your desk\n")
    print("Press Ctrl+C to stop the server")
    print("="*70 + "\n")

    web.run_app(app, host='127.0.0.1', port=8080)

if __name__ == '__main__':
    main()
