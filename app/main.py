#!/usr/bin/env python3
"""
RadCam Spy - BlueOS Extension
Monitors a RadCam IP camera's HiSilicon SoC via telnet.
Samples temperature, voltage, CPU usage, and memory usage.
"""

import json
import logging
import os
import re
import socket
import time
import threading
from datetime import datetime
from pathlib import Path

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

                if prev_snap is not None:
                    cpu_pct = compute_cpu_percent(prev_snap, snap)
                    if cpu_pct is not None:
                        snap["cpu_percent"] = cpu_pct

                f.write(json.dumps(snap) + "\n")
                f.flush()

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
    for f in sorted(LOGS_DIR.glob("*.ndjson"), reverse=True):
        stat = f.stat()
        logs.append({
            "name": f.name,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "samples": sum(1 for _ in open(f)) if stat.st_size > 0 else 0,
        })
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
    logger.info("RadCam Spy starting on port 9850")
    app.run(host="0.0.0.0", port=9850)
