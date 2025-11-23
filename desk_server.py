#!/usr/bin/env python3
"""
Uplift Desk Web Controller
A web interface to control your sit/stand desk with activity tracking.

Uses the desk_control module for reliable BLE communication.
"""

import asyncio
from aiohttp import web
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# Import from our desk_control module
from desk_control import (
    COMMANDS, DESK_CONFIGS, DESK_SERVICE_UUIDS, DeskOpcode,
    get_desk_address, get_cached_config, cache_config, detect_desk_config,
    send_wake_sequence, send_command, create_command_packet,
    log_position, LOG_FILE, CACHE_FILE, CONFIG_CACHE_FILE,
    normalize_uuid_16
)
from bleak import BleakClient, BleakScanner

# Height calibration constants
# BLE reports raw encoder values that need scaling and offset
# Formula: display_mm = (BLE_value / HEIGHT_SCALE_FACTOR) + HEIGHT_BASE_OFFSET_MM
# Calibrated using: sit=30.2" (BLE=11791), stand=45.5" (BLE=50959)
HEIGHT_SCALE_FACTOR = 100.7874  # BLE units per mm
HEIGHT_BASE_OFFSET_MM = 650.09  # Add this to scaled BLE value to get display mm

# Global state
desk_client = None
current_height = 0
connected = False
current_config = None


def notification_handler(sender, data):
    """Handle height notifications from desk"""
    global current_height
    if len(data) >= 8:
        # Protocol: f2 f2 01 03 SS HH HH checksum 7e
        # Byte 0-1: header (f2 f2)
        # Byte 2: opcode (01 = height)
        # Byte 3: payload length (03)
        # Byte 4: status byte
        # Byte 5-6: height value (big-endian) in hundredths of mm
        # Byte 7: checksum
        # Byte 8: trailer (7e)
        if data[0] == 0xf2 and data[1] == 0xf2:
            opcode = data[2]
            if opcode == 0x01:  # Height notification
                height_bytes = data[5:7]
                raw_value = int.from_bytes(height_bytes, byteorder='big')
                # Convert: display_mm = (raw_value / 100) + offset
                current_height = (raw_value / HEIGHT_SCALE_FACTOR) + HEIGHT_BASE_OFFSET_MM


async def connect_to_desk():
    """Connect to the desk using desk_control module"""
    global desk_client, connected, current_config

    address = await get_desk_address()
    if not address:
        raise Exception("Desk not found. Make sure it's powered on and press a button to wake it.")

    desk_client = BleakClient(address, timeout=20.0)
    await desk_client.connect()

    if desk_client.is_connected:
        # Detect desk variant
        current_config = get_cached_config()
        if not current_config:
            current_config = await detect_desk_config(desk_client)
            if current_config:
                cache_config(current_config)
                print(f"Detected desk variant: {current_config.variant_name}")

        if not current_config:
            current_config = DESK_CONFIGS[normalize_uuid_16(0xFF00)]
            print(f"Using default variant: {current_config.variant_name}")

        # Start notifications
        await desk_client.start_notify(current_config.output_char_uuid, notification_handler)
        connected = True
        print(f"Connected to desk at {address}")
    else:
        raise Exception("Failed to connect to desk")


async def disconnect_from_desk():
    """Disconnect from desk"""
    global desk_client, connected, current_config
    if desk_client and desk_client.is_connected:
        if current_config:
            await desk_client.stop_notify(current_config.output_char_uuid)
        await desk_client.disconnect()
        connected = False
        print("Disconnected from desk")


async def send_desk_command(command_name: str):
    """Send a command to the desk"""
    global desk_client, current_config

    if not desk_client or not desk_client.is_connected:
        raise Exception("Not connected to desk")

    if not current_config:
        raise Exception("Desk configuration not detected")

    success = await send_command(desk_client, current_config, command_name)
    if success and command_name in ('sit', 'stand'):
        log_position(command_name)
    return success


def parse_log_file():
    """Parse the desk log file and return activity data"""
    activities = []

    if not LOG_FILE.exists():
        return activities

    with open(LOG_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                timestamp_str, position = line.split(',', 1)
                timestamp = datetime.fromisoformat(timestamp_str)
                activities.append({
                    'timestamp': timestamp_str,
                    'position': position,
                    'date': timestamp.strftime('%Y-%m-%d'),
                    'time': timestamp.strftime('%H:%M'),
                    'hour': timestamp.hour
                })
            except (ValueError, IndexError):
                continue

    return activities


def calculate_daily_stats(activities):
    """Calculate daily sit/stand statistics"""
    daily_stats = defaultdict(lambda: {'sit_count': 0, 'stand_count': 0, 'transitions': 0})

    for activity in activities:
        date = activity['date']
        position = activity['position']

        if position == 'sit':
            daily_stats[date]['sit_count'] += 1
        elif position == 'stand':
            daily_stats[date]['stand_count'] += 1

        daily_stats[date]['transitions'] += 1

    # Convert to list sorted by date
    result = []
    for date in sorted(daily_stats.keys()):
        stats = daily_stats[date]
        result.append({
            'date': date,
            'sit_count': stats['sit_count'],
            'stand_count': stats['stand_count'],
            'transitions': stats['transitions']
        })

    return result


def calculate_hourly_distribution(activities):
    """Calculate hourly distribution of standing"""
    hourly = defaultdict(lambda: {'sit': 0, 'stand': 0})

    for activity in activities:
        hour = activity['hour']
        position = activity['position']
        hourly[hour][position] += 1

    result = []
    for hour in range(24):
        result.append({
            'hour': hour,
            'label': f"{hour:02d}:00",
            'sit': hourly[hour]['sit'],
            'stand': hourly[hour]['stand']
        })

    return result


# Web handlers
async def handle_index(request):
    """Serve the main HTML page"""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Uplift Desk Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-card: #1c2128;
            --bg-hover: #262c36;
            --text-primary: #f0f3f6;
            --text-secondary: #9ca3af;
            --text-muted: #6b7280;
            --accent: #3b82f6;
            --accent-light: #60a5fa;
            --success: #22c55e;
            --border: rgba(255,255,255,0.08);
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Inter', sans-serif;
            background: var(--bg-primary);
            min-height: 100vh;
            color: var(--text-primary);
            -webkit-font-smoothing: antialiased;
        }

        .app-layout {
            display: flex;
            min-height: 100vh;
        }

        /* Left Panel - Controls */
        .sidebar {
            width: 320px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border);
            padding: 24px;
            display: flex;
            flex-direction: column;
            position: sticky;
            top: 0;
            height: 100vh;
            overflow-y: auto;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 32px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }

        .brand-icon {
            width: 36px;
            height: 36px;
            background: linear-gradient(135deg, var(--accent) 0%, #8b5cf6 100%);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 18px;
            color: white;
        }

        .brand-text {
            font-size: 18px;
            font-weight: 600;
        }

        /* Status */
        .status-section {
            background: var(--bg-card);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
        }

        .status-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .status-left {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--text-muted);
            transition: all 0.3s ease;
        }

        .status-dot.connected {
            background: var(--success);
            box-shadow: 0 0 12px var(--success);
        }

        .status-label {
            font-size: 13px;
            color: var(--text-muted);
        }

        .status-value {
            font-size: 13px;
            color: var(--text-secondary);
            font-weight: 500;
        }

        /* Height Display */
        .height-section {
            text-align: center;
            padding: 24px 0;
            margin-bottom: 20px;
        }

        .height-value {
            font-size: 56px;
            font-weight: 700;
            letter-spacing: -2px;
            color: var(--text-primary);
            font-variant-numeric: tabular-nums;
            line-height: 1;
        }

        .height-label {
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 8px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        /* Controls */
        .controls {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .control-group {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }

        button {
            padding: 14px 16px;
            font-size: 14px;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.15s ease;
            color: var(--text-primary);
            background: var(--bg-card);
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            border: 1px solid var(--border);
        }

        button:hover:not(:disabled) {
            background: var(--bg-hover);
            border-color: rgba(255,255,255,0.15);
        }

        button:active:not(:disabled) {
            transform: scale(0.98);
        }

        button:disabled {
            opacity: 0.4;
            cursor: not-allowed;
        }

        .btn-primary {
            background: var(--accent);
            border-color: var(--accent);
            color: white;
        }

        .btn-primary:hover:not(:disabled) {
            background: #2563eb;
            border-color: #2563eb;
        }

        .btn-stop {
            background: #dc2626;
            border-color: #dc2626;
            grid-column: span 2;
        }

        .btn-stop:hover:not(:disabled) {
            background: #b91c1c;
            border-color: #b91c1c;
        }

        .btn-secondary {
            background: transparent;
            font-size: 13px;
            padding: 12px;
        }

        .variant-badge {
            font-size: 10px;
            color: var(--text-muted);
            text-align: center;
            margin-top: auto;
            padding-top: 20px;
            font-family: 'SF Mono', Monaco, monospace;
        }

        /* Main Content */
        .main-content {
            flex: 1;
            padding: 24px;
            overflow-y: auto;
        }

        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }

        .stat-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid var(--border);
        }

        .stat-label {
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .stat-value {
            font-size: 32px;
            font-weight: 700;
            color: var(--text-primary);
            letter-spacing: -1px;
        }

        .stat-trend {
            font-size: 12px;
            color: var(--success);
            margin-top: 4px;
        }

        .charts-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 24px;
        }

        .card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid var(--border);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .card-title {
            font-size: 14px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .chart-container {
            position: relative;
            height: 200px;
        }

        /* Activity List */
        .activity-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid var(--border);
        }

        .activity-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .activity-title {
            font-size: 14px;
            font-weight: 600;
        }

        .activity-list {
            max-height: 280px;
            overflow-y: auto;
        }

        .activity-list::-webkit-scrollbar {
            width: 4px;
        }

        .activity-list::-webkit-scrollbar-thumb {
            background: var(--border);
            border-radius: 2px;
        }

        .activity-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid var(--border);
        }

        .activity-item:last-child {
            border-bottom: none;
        }

        .activity-time {
            font-size: 13px;
            color: var(--text-muted);
        }

        .activity-badge {
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }

        .activity-badge.sit {
            background: rgba(59, 130, 246, 0.15);
            color: var(--accent-light);
        }

        .activity-badge.stand {
            background: rgba(34, 197, 94, 0.15);
            color: #4ade80;
        }

        .empty-state {
            color: var(--text-muted);
            font-size: 13px;
            padding: 32px;
            text-align: center;
        }

        /* Main layout - center + right columns (sidebar is 1/5, these split remaining 4/5 equally) */
        .main-layout {
            display: flex;
            gap: 20px;
        }

        /* Center Column - 3D preview + stats + activity (2/5 of total screen) */
        .center-column {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        /* 3D Desk Visualization */
        .desk-3d-card {
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 20px;
            border: 1px solid var(--border);
            display: flex;
            flex-direction: column;
        }

        .desk-3d-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }

        .desk-3d-title {
            font-size: 14px;
            font-weight: 600;
        }

        .desk-3d-hint {
            font-size: 11px;
            color: var(--text-muted);
        }

        .desk-3d-container {
            height: 280px;
            border-radius: 8px;
            overflow: hidden;
            background: linear-gradient(180deg, #1a1f2e 0%, #0d1117 100%);
        }

        .desk-3d-container canvas {
            width: 100%;
            height: 100%;
        }

        /* Stats Row - horizontal below 3D */
        .stats-row {
            display: flex;
            gap: 12px;
        }

        .stats-row .stat-card {
            flex: 1;
            padding: 16px;
        }

        /* Center column activity */
        .center-column .activity-card {
            flex: 1;
        }

        .center-column .activity-list {
            max-height: 200px;
        }

        /* Right Column - charts (2/5 of total screen, same as center) */
        .right-column {
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .chart-card {
            flex: 1;
            padding: 16px;
            display: flex;
            flex-direction: column;
        }

        .chart-container-small {
            flex: 1;
            min-height: 150px;
        }

        /* Loading */
        .loading-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.6);
            backdrop-filter: blur(4px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }

        .loading-overlay.active {
            display: flex;
        }

        .loading-spinner {
            width: 40px;
            height: 40px;
            border: 3px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Responsive - Tablet */
        @media (max-width: 1024px) {
            .app-layout {
                flex-direction: column;
            }

            .sidebar {
                width: 100%;
                height: auto;
                position: relative;
                border-right: none;
                border-bottom: 1px solid var(--border);
            }

            .main-layout {
                flex-direction: column;
            }

            .center-column,
            .right-column {
                flex: none;
                width: 100%;
            }

            .chart-container-small {
                min-height: 150px;
            }
        }

        /* Responsive - Mobile */
        @media (max-width: 640px) {
            .stats-row {
                flex-direction: column;
            }

            .sidebar {
                padding: 16px;
            }

            .main-content {
                padding: 16px;
            }

            .height-value {
                font-size: 48px;
            }
        }
    </style>
</head>
<body>
    <!-- Loading Overlay -->
    <div class="loading-overlay" id="loadingOverlay">
        <div class="loading-spinner"></div>
    </div>

    <div class="app-layout">
        <!-- Left Sidebar - Controls -->
        <aside class="sidebar">
            <div class="brand">
                <div class="brand-icon">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="2" y="6" width="20" height="3" rx="1"/>
                        <path d="M4 9v9"/>
                        <path d="M20 9v9"/>
                        <path d="M12 9v5"/>
                        <path d="M9 14h6"/>
                    </svg>
                </div>
                <span class="brand-text">Desk Control</span>
            </div>

            <div class="status-section">
                <div class="status-row">
                    <div class="status-left">
                        <span class="status-dot" id="statusDot"></span>
                        <span class="status-label">Status</span>
                    </div>
                    <span class="status-value" id="statusText">Ready</span>
                </div>
            </div>

            <div class="height-section">
                <div class="height-value" id="height">--</div>
                <div class="height-label">Current Height</div>
            </div>

            <div class="controls">
                <div class="control-group">
                    <button id="sitBtn" class="btn-primary" onclick="sendCommand('sit')">
                        Sit
                    </button>
                    <button id="standBtn" class="btn-primary" onclick="sendCommand('stand')">
                        Stand
                    </button>
                </div>

                <div class="control-group">
                    <button id="upBtn" onclick="sendCommand('up')">
                        Up
                    </button>
                    <button id="downBtn" onclick="sendCommand('down')">
                        Down
                    </button>
                </div>

                <div class="control-group">
                    <button id="stopBtn" class="btn-stop" onclick="sendCommand('stop')">
                        Stop
                    </button>
                </div>

                <div class="control-group">
                    <button class="btn-secondary" onclick="sendMemory(3)" id="mem3Btn">
                        Memory 3
                    </button>
                    <button class="btn-secondary" onclick="sendMemory(4)" id="mem4Btn">
                        Memory 4
                    </button>
                </div>
            </div>

            <div class="variant-badge" id="variantBadge"></div>
        </aside>

        <!-- Main Content - 3D Preview + Charts -->
        <main class="main-content">
            <div class="main-layout">
                <!-- Center Column - 3D Desk + Stats + Activity -->
                <div class="center-column">
                    <!-- 3D Desk Visualization -->
                    <div class="desk-3d-card">
                        <div class="desk-3d-header">
                            <span class="desk-3d-title">Desk Preview</span>
                            <span class="desk-3d-hint">Drag to rotate, scroll to zoom</span>
                        </div>
                        <div class="desk-3d-container" id="desk3dContainer"></div>
                    </div>

                    <!-- Today's Stats - Horizontal below 3D -->
                    <div class="stats-row">
                        <div class="stat-card">
                            <div class="stat-label">Sit Today</div>
                            <div class="stat-value" id="todaySit">0</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">Stand Today</div>
                            <div class="stat-value" id="todayStand">0</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">Transitions</div>
                            <div class="stat-value" id="todayTransitions">0</div>
                        </div>
                    </div>

                    <!-- Recent Activity - Below stats -->
                    <div class="activity-card">
                        <div class="activity-header">
                            <span class="activity-title">Recent Activity</span>
                        </div>
                        <div class="activity-list" id="activityList">
                            <div class="empty-state">No activity recorded</div>
                        </div>
                    </div>
                </div>

                <!-- Right Column - Charts -->
                <div class="right-column">
                    <div class="card chart-card">
                        <div class="card-header">
                            <span class="card-title">Daily Activity</span>
                        </div>
                        <div class="chart-container-small">
                            <canvas id="dailyChart"></canvas>
                        </div>
                    </div>

                    <div class="card chart-card">
                        <div class="card-header">
                            <span class="card-title">Hourly Distribution</span>
                        </div>
                        <div class="chart-container-small">
                            <canvas id="hourlyChart"></canvas>
                        </div>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <script>
        let connected = false;
        let dailyChart = null;
        let hourlyChart = null;
        let loadingTimeout = null;
        let commandInProgress = false;

        // Three.js 3D Desk variables
        let scene, camera, renderer, deskGroup, controls;
        let deskTop, leftLeg, rightLeg;
        let targetHeight = 0.5; // Normalized 0-1 (0=sit, 1=stand)
        let currentDeskHeight = 0.5;
        let lastInteractionTime = 0;
        let isUserInteracting = false;
        const MIN_HEIGHT_INCHES = 25;
        const MAX_HEIGHT_INCHES = 50;
        const AUTO_ROTATE_DELAY = 2000; // 2 seconds after user stops interacting

        function initDesk3D() {
            const container = document.getElementById('desk3dContainer');
            if (!container) return;

            const width = container.clientWidth;
            const height = container.clientHeight;

            // Scene
            scene = new THREE.Scene();

            // Camera
            camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
            camera.position.set(4, 3, 4);

            // Renderer
            renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
            renderer.setSize(width, height);
            renderer.setPixelRatio(window.devicePixelRatio);
            renderer.setClearColor(0x000000, 0);
            container.appendChild(renderer.domElement);

            // OrbitControls for drag/zoom
            controls = new THREE.OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.05;
            controls.enablePan = false;
            controls.minDistance = 3;
            controls.maxDistance = 10;
            controls.maxPolarAngle = Math.PI / 2; // Don't go below floor
            controls.target.set(0, 1.5, 0);
            controls.autoRotate = true;
            controls.autoRotateSpeed = 1.0;

            // Track user interaction
            controls.addEventListener('start', () => {
                isUserInteracting = true;
                controls.autoRotate = false;
            });
            controls.addEventListener('end', () => {
                isUserInteracting = false;
                lastInteractionTime = Date.now();
            });

            // Lighting
            const ambientLight = new THREE.AmbientLight(0xffffff, 0.4);
            scene.add(ambientLight);

            const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
            directionalLight.position.set(5, 10, 5);
            scene.add(directionalLight);

            const backLight = new THREE.DirectionalLight(0x3b82f6, 0.3);
            backLight.position.set(-5, 5, -5);
            scene.add(backLight);

            // Create desk group
            deskGroup = new THREE.Group();

            // Materials
            const deskTopMaterial = new THREE.MeshPhongMaterial({
                color: 0x4a3728, // Warm wood brown
                shininess: 30
            });
            const legMaterial = new THREE.MeshPhongMaterial({
                color: 0x2a2a2a,
                shininess: 50
            });
            const accentMaterial = new THREE.MeshPhongMaterial({
                color: 0x3b82f6,
                shininess: 80
            });

            // Desk top (wide rectangular surface)
            const topGeometry = new THREE.BoxGeometry(3, 0.08, 1.5);
            deskTop = new THREE.Mesh(topGeometry, deskTopMaterial);
            deskTop.position.y = 2;
            deskGroup.add(deskTop);

            // Desk top accent edge (front)
            const edgeGeometry = new THREE.BoxGeometry(3.02, 0.02, 0.02);
            const frontEdge = new THREE.Mesh(edgeGeometry, accentMaterial);
            frontEdge.position.set(0, -0.03, 0.75);
            deskTop.add(frontEdge);

            // Left leg (vertical column)
            const legGeometry = new THREE.BoxGeometry(0.12, 2, 0.12);
            leftLeg = new THREE.Mesh(legGeometry, legMaterial);
            leftLeg.position.set(-1.2, 1, 0);
            deskGroup.add(leftLeg);

            // Right leg
            rightLeg = new THREE.Mesh(legGeometry, legMaterial);
            rightLeg.position.set(1.2, 1, 0);
            deskGroup.add(rightLeg);

            // Left leg foot
            const footGeometry = new THREE.BoxGeometry(0.3, 0.05, 0.8);
            const leftFoot = new THREE.Mesh(footGeometry, legMaterial);
            leftFoot.position.set(-1.2, 0.025, 0);
            deskGroup.add(leftFoot);

            // Right leg foot
            const rightFoot = new THREE.Mesh(footGeometry, legMaterial);
            rightFoot.position.set(1.2, 0.025, 0);
            deskGroup.add(rightFoot);

            // Floor grid
            const gridHelper = new THREE.GridHelper(8, 20, 0x333333, 0x222222);
            gridHelper.position.y = 0;
            scene.add(gridHelper);

            scene.add(deskGroup);

            // Handle resize
            window.addEventListener('resize', onWindowResize);

            // Start animation loop
            animate();
        }

        function onWindowResize() {
            const container = document.getElementById('desk3dContainer');
            if (!container || !camera || !renderer) return;

            const width = container.clientWidth;
            const height = container.clientHeight;

            camera.aspect = width / height;
            camera.updateProjectionMatrix();
            renderer.setSize(width, height);
        }

        function updateDeskHeight(heightInches) {
            // Convert inches to normalized value (0-1)
            const normalized = (heightInches - MIN_HEIGHT_INCHES) / (MAX_HEIGHT_INCHES - MIN_HEIGHT_INCHES);
            targetHeight = Math.max(0, Math.min(1, normalized));
        }

        function animate() {
            requestAnimationFrame(animate);

            // Re-enable auto-rotate after delay
            if (!isUserInteracting && !controls.autoRotate) {
                if (Date.now() - lastInteractionTime > AUTO_ROTATE_DELAY) {
                    controls.autoRotate = true;
                }
            }

            // Update controls
            if (controls) {
                controls.update();
            }

            // Smooth height animation
            currentDeskHeight += (targetHeight - currentDeskHeight) * 0.08;

            // Update desk geometry based on height
            if (deskTop && leftLeg && rightLeg) {
                // Scale height from 1.5 (sit) to 2.5 (stand)
                const deskY = 1.5 + currentDeskHeight * 1.0;
                deskTop.position.y = deskY;

                // Adjust leg heights
                const legHeight = deskY;
                leftLeg.scale.y = legHeight / 2;
                leftLeg.position.y = legHeight / 2;
                rightLeg.scale.y = legHeight / 2;
                rightLeg.position.y = legHeight / 2;
            }

            if (renderer && scene && camera) {
                renderer.render(scene, camera);
            }
        }

        // Initialize 3D desk when DOM is ready
        document.addEventListener('DOMContentLoaded', function() {
            initDesk3D();
        });

        async function updateStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();

                connected = data.connected;
                const statusDot = document.getElementById('statusDot');
                const statusText = document.getElementById('statusText');

                statusDot.className = `status-dot ${connected ? 'connected' : ''}`;
                statusText.textContent = connected ? 'Connected' : 'Ready';

                const height = data.height;
                if (connected && height) {
                    const inches = (height / 25.4).toFixed(1);
                    document.getElementById('height').textContent = `${inches}"`;
                    // Update 3D desk visualization
                    updateDeskHeight(parseFloat(inches));
                } else {
                    document.getElementById('height').textContent = '--';
                }

                if (data.variant) {
                    document.getElementById('variantBadge').textContent = data.variant;
                }
            } catch (e) {
                console.error('Error updating status:', e);
            }
        }

        function showDelayedLoading(show) {
            // Only show spinner if operation takes more than 3 seconds
            if (show) {
                loadingTimeout = setTimeout(() => {
                    document.getElementById('loadingOverlay').classList.add('active');
                }, 3000);
            } else {
                if (loadingTimeout) {
                    clearTimeout(loadingTimeout);
                    loadingTimeout = null;
                }
                document.getElementById('loadingOverlay').classList.remove('active');
            }
        }

        function setButtonsDisabled(disabled) {
            const buttons = ['sitBtn', 'standBtn', 'upBtn', 'downBtn', 'stopBtn', 'mem3Btn', 'mem4Btn'];
            buttons.forEach(id => {
                document.getElementById(id).disabled = disabled;
            });
        }

        async function sendCommand(cmd) {
            if (commandInProgress) return;
            commandInProgress = true;

            setButtonsDisabled(true);
            showDelayedLoading(true);

            try {
                const response = await fetch('/api/command', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ command: cmd })
                });
                const data = await response.json();

                if (!data.success) {
                    if (data.needed_connection) {
                        // Connection was needed but failed
                        console.error('Connection failed:', data.error);
                    } else {
                        console.error('Command failed:', data.error);
                    }
                }

                // Refresh activity data and status after command
                if (cmd === 'sit' || cmd === 'stand') {
                    setTimeout(loadActivityData, 500);
                }
                await updateStatus();
            } catch (e) {
                console.error('Error sending command:', e);
            }

            showDelayedLoading(false);
            setButtonsDisabled(false);
            commandInProgress = false;
        }

        async function sendMemory(slot) {
            const cmdMap = { 3: 'mem3', 4: 'mem4' };
            await sendCommand(cmdMap[slot] || 'stop');
        }

        async function loadActivityData() {
            try {
                const response = await fetch('/api/activity');
                const data = await response.json();

                updateDailyChart(data.daily_stats);
                updateHourlyChart(data.hourly_distribution);
                updateActivityList(data.recent);
                updateTodayStats(data.today);
            } catch (e) {
                console.error('Error loading activity data:', e);
            }
        }

        function updateDailyChart(dailyStats) {
            const ctx = document.getElementById('dailyChart').getContext('2d');

            // Get last 7 days
            const last7 = dailyStats.slice(-7);
            const labels = last7.map(d => {
                const date = new Date(d.date);
                return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
            });

            if (dailyChart) {
                dailyChart.destroy();
            }

            dailyChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Sit',
                            data: last7.map(d => d.sit_count),
                            backgroundColor: 'rgba(59, 130, 246, 0.8)',
                            borderRadius: 3,
                            barThickness: 16
                        },
                        {
                            label: 'Stand',
                            data: last7.map(d => d.stand_count),
                            backgroundColor: 'rgba(34, 197, 94, 0.8)',
                            borderRadius: 3,
                            barThickness: 16
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {
                            position: 'top',
                            labels: { color: '#5c5c66', font: { size: 11 } }
                        }
                    },
                    scales: {
                        x: {
                            stacked: true,
                            grid: { color: 'rgba(255,255,255,0.04)' },
                            ticks: { color: '#5c5c66', font: { size: 10 } }
                        },
                        y: {
                            stacked: true,
                            grid: { color: 'rgba(255,255,255,0.04)' },
                            ticks: { color: '#5c5c66', font: { size: 10 } }
                        }
                    }
                }
            });
        }

        function updateHourlyChart(hourlyData) {
            const ctx = document.getElementById('hourlyChart').getContext('2d');

            // Filter to working hours (6am - 10pm)
            const workingHours = hourlyData.filter(h => h.hour >= 6 && h.hour <= 22);

            if (hourlyChart) {
                hourlyChart.destroy();
            }

            hourlyChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: workingHours.map(h => h.label),
                    datasets: [
                        {
                            label: 'Stand',
                            data: workingHours.map(h => h.stand),
                            borderColor: 'rgba(34, 197, 94, 0.8)',
                            backgroundColor: 'rgba(34, 197, 94, 0.1)',
                            fill: true,
                            tension: 0.4,
                            borderWidth: 2,
                            pointRadius: 0
                        },
                        {
                            label: 'Sit',
                            data: workingHours.map(h => h.sit),
                            borderColor: 'rgba(59, 130, 246, 0.8)',
                            backgroundColor: 'rgba(59, 130, 246, 0.1)',
                            fill: true,
                            tension: 0.4,
                            borderWidth: 2,
                            pointRadius: 0
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        intersect: false,
                        mode: 'index'
                    },
                    plugins: {
                        legend: {
                            position: 'top',
                            labels: { color: '#5c5c66', font: { size: 11 } }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: 'rgba(255,255,255,0.04)' },
                            ticks: { color: '#5c5c66', font: { size: 10 } }
                        },
                        y: {
                            grid: { color: 'rgba(255,255,255,0.04)' },
                            ticks: { color: '#5c5c66', font: { size: 10 } }
                        }
                    }
                }
            });
        }

        function updateActivityList(recent) {
            const list = document.getElementById('activityList');

            if (!recent || recent.length === 0) {
                list.innerHTML = '<div class="empty-state">No activity recorded</div>';
                return;
            }

            list.innerHTML = recent.slice(0, 20).map(activity => `
                <div class="activity-item">
                    <span class="activity-time">${activity.time} &middot; ${activity.date}</span>
                    <span class="activity-badge ${activity.position}">${activity.position}</span>
                </div>
            `).join('');
        }

        function updateTodayStats(today) {
            document.getElementById('todaySit').textContent = today?.sit_count || 0;
            document.getElementById('todayStand').textContent = today?.stand_count || 0;
            document.getElementById('todayTransitions').textContent = today?.transitions || 0;
        }

        let statusPollInterval = null;

        function startStatusPolling() {
            // Poll status every 500ms while connected (for live height updates)
            if (!statusPollInterval) {
                statusPollInterval = setInterval(updateStatus, 500);
            }
        }

        function stopStatusPolling() {
            if (statusPollInterval) {
                clearInterval(statusPollInterval);
                statusPollInterval = null;
            }
        }

        // Initial load
        updateStatus();
        loadActivityData();

        // Poll status while connected to get live height updates
        setInterval(() => {
            if (connected) {
                updateStatus();
            }
        }, 1000);
    </script>
</body>
</html>
"""
    return web.Response(text=html, content_type='text/html')


async def handle_status(request):
    """Return current status"""
    return web.json_response({
        'connected': connected,
        'height': current_height if connected else None,
        'variant': current_config.variant_name if current_config else None
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


async def ensure_connected():
    """Ensure we're connected to the desk, connecting if needed."""
    global desk_client, connected

    if connected and desk_client and desk_client.is_connected:
        return True

    # Need to connect
    try:
        await connect_to_desk()
        return True
    except Exception as e:
        print(f"Auto-connect failed: {e}")
        return False


async def handle_command(request):
    """Handle desk commands with implicit connection"""
    try:
        data = await request.json()
        command = data.get('command')

        # Map mem3/mem4 to actual command names if needed
        cmd_map = {
            'mem3': 'mem3',
            'mem4': 'mem4',
        }
        cmd = cmd_map.get(command, command)

        # Auto-connect if needed
        if not await ensure_connected():
            return web.json_response({
                'success': False,
                'error': 'Could not connect to desk. Make sure it is powered on.',
                'needed_connection': True
            })

        # For mem3/mem4, we need to add them to COMMANDS if not present
        if cmd == 'mem3' and cmd not in COMMANDS:
            success = await send_memory_command(3)
        elif cmd == 'mem4' and cmd not in COMMANDS:
            success = await send_memory_command(4)
        else:
            success = await send_desk_command(cmd)

        return web.json_response({'success': success})
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)})


async def send_memory_command(slot: int):
    """Send memory slot command"""
    global desk_client, current_config

    if not desk_client or not desk_client.is_connected or not current_config:
        return False

    # Memory slots: 1=0x05, 2=0x06, 3=0x07, 4=0x08
    opcode = 0x04 + slot
    command = create_command_packet(opcode)

    try:
        if current_config.requires_wake:
            await send_wake_sequence(desk_client, current_config.input_char_uuid)

        await desk_client.write_gatt_char(current_config.input_char_uuid, command, response=False)
        await asyncio.sleep(0.15)
        await desk_client.write_gatt_char(current_config.input_char_uuid, command, response=False)
        return True
    except Exception as e:
        print(f"Error sending memory command: {e}")
        return False


async def handle_activity(request):
    """Return activity data for charts"""
    activities = parse_log_file()
    daily_stats = calculate_daily_stats(activities)
    hourly_dist = calculate_hourly_distribution(activities)

    # Get today's stats
    today = datetime.now().strftime('%Y-%m-%d')
    today_stats = next((d for d in daily_stats if d['date'] == today),
                       {'sit_count': 0, 'stand_count': 0, 'transitions': 0})

    # Recent activities (last 20, reversed for most recent first)
    recent = list(reversed(activities[-20:])) if activities else []

    return web.json_response({
        'daily_stats': daily_stats,
        'hourly_distribution': hourly_dist,
        'recent': recent,
        'today': today_stats
    })


async def on_shutdown(app):
    """Cleanup on shutdown"""
    await disconnect_from_desk()


def main():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/status', handle_status)
    app.router.add_get('/api/activity', handle_activity)
    app.router.add_post('/api/connect', handle_connect)
    app.router.add_post('/api/disconnect', handle_disconnect)
    app.router.add_post('/api/command', handle_command)
    app.on_shutdown.append(on_shutdown)

    print("="*70)
    print("UPLIFT DESK WEB CONTROLLER")
    print("="*70)
    print(f"\nServer starting at http://localhost:8080")
    print(f"Open this URL in your browser to control your desk\n")
    print("Press Ctrl+C to stop the server")
    print("="*70 + "\n")

    web.run_app(app, host='127.0.0.1', port=8080)


if __name__ == '__main__':
    main()
