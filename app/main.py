#!/usr/bin/env python3
"""
RadCam Spy - BlueOS Extension
Monitors a RadCam IP camera's HiSilicon SoC via telnet.
Samples temperature, voltage, CPU usage, and memory usage.
"""

import asyncio
import json
import logging
import os
import re
import socket
import time
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

import websockets

from flask import Flask, jsonify, request, send_file, send_from_directory, abort

app = Flask(__name__, static_folder="static")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("radcam-spy")

DATA_DIR = Path("/app/data")
LOGS_DIR = DATA_DIR / "logs"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "camera_ip": "192.168.2.10",
    "telnet_user": "root",
    "telnet_password": "",
    "interval": 2.0,
    "cockpit_vars": ["temp_c", "core_volt"],
}

# ── Global monitor state ────────────────────────────────────────────────────

monitor_lock = threading.Lock()
monitor_thread: threading.Thread | None = None
monitor_stop_event = threading.Event()
monitor_state = {
    "running": False,
    "error": None,
    "samples": 0,
    "start_time": None,
    "last_sample": None,
    "current_log": None,
}


# ── Cockpit WebSocket server ─────────────────────────────────────────────────

ws_clients: set = set()
ws_loop: asyncio.AbstractEventLoop | None = None


async def ws_handler(websocket):
    """Handle a single Cockpit WebSocket client connection."""
    ws_clients.add(websocket)
    logger.info("Cockpit WS client connected: %s", websocket.remote_address)
    try:
        await websocket.send("radcam-connection-status=connected")
        async for _ in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        ws_clients.discard(websocket)
        logger.info("Cockpit WS client disconnected")


def ws_broadcast(snap):
    """Send selected snapshot fields to all connected Cockpit WS clients."""
    if not ws_clients or ws_loop is None:
        return

    settings = load_settings()
    selected = settings.get("cockpit_vars", [])
    if not selected:
        return

    enriched = dict(snap)
    if "mem_used_percent" not in enriched:
        total = enriched.get("mem_memtotal_kb", 0)
        free = enriched.get("mem_memfree_kb", 0)
        if total > 0:
            enriched["mem_used_percent"] = round(100.0 * (total - free) / total, 1)

    messages = []
    for key in selected:
        val = enriched.get(key)
        if val is not None:
            ws_key = "radcam-" + key.replace("_", "-")
            messages.append(f"{ws_key}={val}")

    if not messages:
        return

    async def _send():
        dead = set()
        for client in ws_clients.copy():
            try:
                for msg in messages:
                    await client.send(msg)
            except Exception:
                dead.add(client)
        ws_clients.difference_update(dead)

    asyncio.run_coroutine_threadsafe(_send(), ws_loop)


def start_ws_server():
    """Start the WebSocket server on a background daemon thread."""
    global ws_loop

    async def _serve():
        async with websockets.serve(ws_handler, "0.0.0.0", 9851):
            logger.info("Cockpit WebSocket server started on ws://0.0.0.0:9851")
            await asyncio.Future()

    def _run():
        global ws_loop
        ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_loop)
        ws_loop.run_until_complete(_serve())

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ── CameraTelnet (from camera_monitor.py) ───────────────────────────────────

class CameraTelnet:
    """Minimal telnet client for HiSilicon cameras (no telnetlib needed)."""

    def __init__(self, host, port=23, timeout=5):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))

    def read_until(self, marker, timeout=5):
        data = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if marker.encode() in data:
                    break
            except socket.timeout:
                break
        return data.decode(errors="replace")

    def cmd(self, command, marker="# ", timeout=3):
        """Send a command and wait for the shell prompt."""
        self.sock.sendall((command + "\n").encode())
        data = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if marker.encode() in data:
                    break
            except socket.timeout:
                break
        return data.decode(errors="replace")

    def login(self, user, password):
        self.read_until("login:", timeout=5)
        self.sock.sendall((user + "\n").encode())
        self.read_until("assword:", timeout=3)
        self.sock.sendall((password + "\n").encode())
        resp = self.read_until("# ", timeout=5)
        return "#" in resp or "Welcome" in resp

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ── Parsers (from camera_monitor.py) ────────────────────────────────────────

def parse_pm(text):
    """Parse /proc/umap/pm output for temperature and voltages."""
    result = {}
    m = re.search(r"cur_temp:\s+(-?\d+)", text)
    if m:
        result["temp_c"] = int(m.group(1))
    for name in ("core_cur_volt", "cpu_cur_volt", "npu_cur_volt"):
        m = re.search(rf"{name}:\s+(\d+)", text)
        if m:
            result[name.replace("_cur_", "_")] = int(m.group(1))
    for name in ("core_temp_comp", "cpu_temp_comp", "npu_temp_comp"):
        m = re.search(rf"{name}:\s+(-?\d+)", text)
        if m:
            result[name] = int(m.group(1))
    return result


def parse_stat(text):
    """Parse /proc/stat cpu line into total and busy jiffies."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            vals = [int(x) for x in parts[1:]]
            total = sum(vals[:8]) if len(vals) >= 8 else sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return {"cpu_total": total, "cpu_busy": total - idle}
    return {}


def parse_meminfo(text):
    """Parse /proc/meminfo for key fields."""
    result = {}
    for line in text.splitlines():
        for key in ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached"):
            if line.startswith(key + ":"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        result[f"mem_{key.lower()}_kb"] = int(parts[1])
                    except ValueError:
                        pass
    return result


def snapshot(tn):
    """Take a single snapshot of the camera's SoC state."""
    snap = {"ts": time.time()}

    pm_text = tn.cmd("cat /proc/umap/pm", timeout=2)
    snap.update(parse_pm(pm_text))

    stat_text = tn.cmd("head -1 /proc/stat", timeout=2)
    snap.update(parse_stat(stat_text))

    mem_text = tn.cmd(
        "grep -E '^(MemTotal|MemFree|MemAvailable|Buffers|Cached):' /proc/meminfo",
        timeout=2,
    )
    snap.update(parse_meminfo(mem_text))

    return snap


# ── ISP info (HTTP) ─────────────────────────────────────────────────────────

def fetch_isp_info(host):
    """Fetch and parse ISP info from the camera's HTTP API."""
    url = f"http://{host}/action/getISPInfo"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return {}

    isp_str = data.get("isp_info", "")
    result = {}
    for key, field in (
        ("ISO", "isp_iso"),
        ("AGain", "isp_again"),
        ("DGain", "isp_dgain"),
        ("ISPDGain", "isp_ispdgain"),
        ("ExpTime", "isp_exptime"),
        ("Exposure", "isp_exposure"),
        ("HistError", "isp_histerror"),
    ):
        m = re.search(rf"{key}:(\d+)", isp_str)
        if m:
            result[field] = int(m.group(1))
    return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings():
    ensure_dirs()
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            merged = {**DEFAULT_SETTINGS, **saved}
            return merged
        except (json.JSONDecodeError, IOError):
            logger.warning("Corrupt settings file, using defaults")
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    ensure_dirs()
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


def compute_cpu_percent(prev, curr):
    """Compute CPU% between two snapshots that have cpu_total/cpu_busy."""
    dt = curr.get("cpu_total", 0) - prev.get("cpu_total", 0)
    db = curr.get("cpu_busy", 0) - prev.get("cpu_busy", 0)
    if dt <= 0:
        return None
    return round(100.0 * db / dt, 1)


# ── Monitor thread ──────────────────────────────────────────────────────────

def monitor_loop():
    global monitor_state
    settings = load_settings()
    host = settings["camera_ip"]
    user = settings["telnet_user"]
    password = settings["telnet_password"]
    interval = max(0.5, settings.get("interval", 2.0))

    logger.info("Monitor connecting to %s ...", host)

    try:
        tn = CameraTelnet(host)
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        with monitor_lock:
            monitor_state["running"] = False
            monitor_state["error"] = f"Connection failed: {exc}"
        logger.error("Cannot connect to camera: %s", exc)
        return

    if not tn.login(user, password):
        tn.close()
        with monitor_lock:
            monitor_state["running"] = False
            monitor_state["error"] = "Telnet login failed"
        logger.error("Telnet login failed for %s@%s", user, host)
        return

    logger.info("Connected to camera at %s", host)

    ensure_dirs()
    log_filename = f"radcam_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ndjson"
    log_path = LOGS_DIR / log_filename

    with monitor_lock:
        monitor_state["error"] = None
        monitor_state["current_log"] = log_filename

    prev_snap = None

    try:
        with open(log_path, "w") as f:
            while not monitor_stop_event.is_set():
                snap = snapshot(tn)
                snap.update(fetch_isp_info(host))

                if prev_snap is not None:
                    cpu_pct = compute_cpu_percent(prev_snap, snap)
                    if cpu_pct is not None:
                        snap["cpu_percent"] = cpu_pct

                f.write(json.dumps(snap) + "\n")
                f.flush()

                ws_broadcast(snap)

                with monitor_lock:
                    monitor_state["samples"] += 1
                    monitor_state["last_sample"] = snap

                prev_snap = snap

                elapsed = time.time() - snap["ts"]
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0 and not monitor_stop_event.wait(sleep_time):
                    pass
    except Exception as exc:
        logger.error("Monitor error: %s", exc)
        with monitor_lock:
            monitor_state["error"] = str(exc)
    finally:
        tn.close()
        with monitor_lock:
            monitor_state["running"] = False
        logger.info("Monitor stopped. %d samples -> %s", monitor_state["samples"], log_filename)


# ── API: Static pages ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/widget")
def widget():
    return send_from_directory(app.static_folder, "widget.html")


@app.route("/register_service")
def register_service():
    return send_from_directory(app.static_folder, "register_service")


@app.route("/icon.png")
def icon():
    return send_from_directory(app.static_folder, "icon.png")


# ── API: Settings ────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    settings = load_settings()
    safe = {**settings, "telnet_password": "••••••••" if settings.get("telnet_password") else ""}
    return jsonify(safe)


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json(force=True)
    current = load_settings()

    if "camera_ip" in data:
        current["camera_ip"] = data["camera_ip"].strip()
    if "telnet_user" in data:
        current["telnet_user"] = data["telnet_user"].strip()
    if "telnet_password" in data and data["telnet_password"] not in ("", "••••••••"):
        current["telnet_password"] = data["telnet_password"]
    if "interval" in data:
        try:
            current["interval"] = max(0.5, float(data["interval"]))
        except (ValueError, TypeError):
            pass
    if "cockpit_vars" in data and isinstance(data["cockpit_vars"], list):
        current["cockpit_vars"] = [str(v) for v in data["cockpit_vars"]]

    save_settings(current)
    logger.info("Settings updated")
    return jsonify({"success": True})


# ── API: Monitor control ────────────────────────────────────────────────────

@app.route("/api/start", methods=["POST"])
def start_monitor():
    global monitor_thread, monitor_state

    with monitor_lock:
        if monitor_state["running"]:
            return jsonify({"success": False, "message": "Already monitoring"}), 400

    settings = load_settings()
    if not settings.get("telnet_password"):
        return jsonify({"success": False, "message": "Telnet password not configured. Set it in Settings first."}), 400

    monitor_stop_event.clear()

    with monitor_lock:
        monitor_state = {
            "running": True,
            "error": None,
            "samples": 0,
            "start_time": datetime.now().isoformat(),
            "last_sample": None,
            "current_log": None,
        }

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    return jsonify({"success": True})


@app.route("/api/stop", methods=["POST"])
def stop_monitor():
    with monitor_lock:
        if not monitor_state["running"]:
            return jsonify({"success": True, "message": "Not running"})

    monitor_stop_event.set()

    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=10)

    return jsonify({"success": True})


@app.route("/api/status", methods=["GET"])
def get_status():
    with monitor_lock:
        state = dict(monitor_state)
        if state.get("last_sample"):
            state["last_sample"] = dict(state["last_sample"])
    return jsonify(state)


# ── API: Log files ───────────────────────────────────────────────────────────

@app.route("/api/logs", methods=["GET"])
def list_logs():
    ensure_dirs()
    logs = []
    for f in LOGS_DIR.glob("*.ndjson"):
        st = f.stat()
        sample_count = 0
        if st.st_size > 0:
            with open(f) as fh:
                sample_count = sum(1 for _ in fh)
        logs.append({
            "name": f.name,
            "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "samples": sample_count,
        })
    logs.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify({"logs": logs})


@app.route("/api/logs/<name>/download", methods=["GET"])
def download_log(name):
    ensure_dirs()
    path = LOGS_DIR / name
    if not path.exists() or not path.name.endswith(".ndjson"):
        abort(404)
    return send_file(path, as_attachment=True)


@app.route("/api/logs/<name>/data", methods=["GET"])
def log_data(name):
    ensure_dirs()
    path = LOGS_DIR / name
    if not path.exists() or not path.name.endswith(".ndjson"):
        abort(404)

    records = []
    prev = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if prev is not None and "cpu_percent" not in rec:
                cpu_pct = compute_cpu_percent(prev, rec)
                if cpu_pct is not None:
                    rec["cpu_percent"] = cpu_pct

            if "mem_memtotal_kb" in rec and "mem_memfree_kb" in rec:
                total = rec["mem_memtotal_kb"]
                free = rec["mem_memfree_kb"]
                rec["mem_used_percent"] = round(100.0 * (total - free) / total, 1) if total > 0 else 0

            records.append(rec)
            prev = rec

    return jsonify({"records": records})


@app.route("/api/live", methods=["GET"])
def live_data():
    """Return recent records from the active log for live charting."""
    with monitor_lock:
        log_name = monitor_state.get("current_log")
        running = monitor_state["running"]

    if not log_name or not running:
        return jsonify({"records": []})

    path = LOGS_DIR / log_name
    if not path.exists():
        return jsonify({"records": []})

    limit = request.args.get("limit", 120, type=int)

    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(line)

    lines = lines[-limit:]

    records = []
    prev = None
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        if prev is not None and "cpu_percent" not in rec:
            cpu_pct = compute_cpu_percent(prev, rec)
            if cpu_pct is not None:
                rec["cpu_percent"] = cpu_pct

        if "mem_memtotal_kb" in rec and "mem_memfree_kb" in rec:
            total = rec["mem_memtotal_kb"]
            free = rec["mem_memfree_kb"]
            rec["mem_used_percent"] = round(100.0 * (total - free) / total, 1) if total > 0 else 0

        records.append(rec)
        prev = rec

    return jsonify({"records": records})


@app.route("/api/logs/<name>", methods=["DELETE"])
def delete_log(name):
    ensure_dirs()
    path = LOGS_DIR / name
    if not path.exists() or not path.name.endswith(".ndjson"):
        abort(404)

    with monitor_lock:
        if monitor_state.get("current_log") == name and monitor_state["running"]:
            return jsonify({"success": False, "message": "Cannot delete the active log file"}), 400

    path.unlink()
    return jsonify({"success": True})


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_dirs()
    start_ws_server()
    logger.info("RadCam Spy starting on port 9850 (WS on 9851)")
    app.run(host="0.0.0.0", port=9850)
