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
from websockets.datastructures import Headers as WsHeaders
from websockets.http11 import Response as WsResponse

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
    "last_successful_login": None,
}

# Auto-start behaviour: if credentials have ever worked, retry starting on
# extension launch for this many seconds (camera may not be up yet at boot).
AUTO_START_DURATION_SEC = 5 * 60
AUTO_START_RETRY_INTERVAL_SEC = 10.0
# After kicking off the monitor thread, wait up to this long for either a
# real sample (success) or the thread to exit (failure) before retrying.
AUTO_START_VERIFY_SEC = 15.0

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

# ── Auto-start state ─────────────────────────────────────────────────────────

auto_start_lock = threading.Lock()
auto_start_thread: threading.Thread | None = None
auto_start_stop_event = threading.Event()
auto_start_state = {
    "active": False,
    "start_time": None,
    "deadline": None,
    "attempts": 0,
    "last_attempt_error": None,
}


# ── Cockpit WebSocket server ─────────────────────────────────────────────────

ws_clients: set = set()
ws_loop: asyncio.AbstractEventLoop | None = None


def ws_process_request(connection, request):
    """Return a simple HTTP 200 for non-WebSocket probes (BlueOS service scanner)."""
    if "Upgrade" not in request.headers:
        return WsResponse(200, "OK", WsHeaders(), b"RadCam Spy WebSocket endpoint\n")


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
        async with websockets.serve(ws_handler, "0.0.0.0", 9851, process_request=ws_process_request):
            logger.info("Cockpit WebSocket server started on ws://0.0.0.0:9851")
            await asyncio.Future()

    def _run():
        global ws_loop
        ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(ws_loop)
        ws_loop.run_until_complete(_serve())

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ── Mavlink2Rest integration ────────────────────────────────────────────────

MAVLINK_NAMES = {
    # Camera SoC
    "temp_c":            "RC_TEMP",
    "core_volt":         "RC_CVOLT",
    "cpu_volt":          "RC_CPUVLT",
    "npu_volt":          "RC_NPUVLT",
    "cpu_percent":       "RC_CPU",
    "mem_used_percent":  "RC_MEM",
    "cpu_freq_mhz":      "RC_FREQ",
    "uptime_sec":        "RC_UPTM",
    "rtsp_clients":      "RC_RTSP",
    "net_tx_bytes":      "RC_TXBY",
    "net_tx_errors":     "RC_TXERR",
    "isp_iso":           "RC_ISO",
    "isp_again":         "RC_AGAIN",
    "isp_dgain":         "RC_DGAIN",
    "isp_ispdgain":      "RC_IDGAIN",
    "isp_exptime":       "RC_EXPTM",
    "isp_exposure":      "RC_EXPO",
    "isp_histerror":     "RC_HSTER",
    # Pi4 host
    "pi4_cpu_percent":   "P4_CPU",
    "pi4_cpu_temp_c":    "P4_TEMP",
    "pi4_disk_free_mb":  "P4_DISK",
    "pi4_net_rx_errors": "P4_RXERR",
}

MAVLINK_ENDPOINTS = [
    "http://host.docker.internal/mavlink2rest/mavlink",
    "http://host.docker.internal:6040/v1/mavlink",
    "http://192.168.2.2/mavlink2rest/mavlink",
    "http://localhost/mavlink2rest/mavlink",
    "http://blueos.local/mavlink2rest/mavlink",
]

_mavlink_endpoint_cache: str | None = None


def send_to_mavlink(name: str, value: float) -> bool:
    """Send a NAMED_VALUE_FLOAT to Mavlink2Rest."""
    global _mavlink_endpoint_cache

    name_array = [name[i] if i < len(name) else "\u0000" for i in range(10)]
    payload = json.dumps({
        "header": {"system_id": 255, "component_id": 0, "sequence": 0},
        "message": {
            "type": "NAMED_VALUE_FLOAT",
            "time_boot_ms": 0,
            "value": float(value),
            "name": name_array,
        },
    }).encode()

    endpoints = ([_mavlink_endpoint_cache] if _mavlink_endpoint_cache else []) + MAVLINK_ENDPOINTS
    for endpoint in endpoints:
        try:
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    _mavlink_endpoint_cache = endpoint
                    return True
                else:
                    logger.warning("Mavlink POST to %s returned status %s", endpoint, resp.status)
        except Exception as exc:
            logger.warning("Mavlink POST to %s failed: %s", endpoint, exc)
            continue

    logger.error("Could not send %s=%.4f to any Mavlink2Rest endpoint", name, value)
    return False


def mavlink_broadcast(snap: dict):
    """Send checked cockpit_vars from the snapshot to Mavlink2Rest."""
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

    for key in selected:
        val = enriched.get(key)
        mav_name = MAVLINK_NAMES.get(key)
        if val is not None and mav_name is not None:
            try:
                send_to_mavlink(mav_name, float(val))
            except (ValueError, TypeError):
                pass


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


def parse_meminfo(text, prefix="mem"):
    """Parse /proc/meminfo for key fields. prefix controls field naming."""
    result = {}
    for line in text.splitlines():
        for key in ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached"):
            if line.startswith(key + ":"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        result[f"{prefix}_{key.lower()}_kb"] = int(parts[1])
                    except ValueError:
                        pass
    return result


def parse_net_dev(text, iface="eth0", prefix="net"):
    """Parse /proc/net/dev for a given interface. Returns TX or RX stats."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(iface + ":"):
            parts = line.split()
            if len(parts) >= 11:
                try:
                    return {
                        f"{prefix}_rx_bytes": int(parts[1]),
                        f"{prefix}_rx_errors": int(parts[3]),
                        f"{prefix}_rx_dropped": int(parts[4]),
                        f"{prefix}_tx_bytes": int(parts[9]),
                        f"{prefix}_tx_packets": int(parts[10]),
                        f"{prefix}_tx_errors": int(parts[11]) if len(parts) > 11 else 0,
                        f"{prefix}_tx_dropped": int(parts[12]) if len(parts) > 12 else 0,
                    }
                except (ValueError, IndexError):
                    pass
    return {}


def parse_uptime(text):
    """Parse /proc/uptime for seconds since boot."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 1:
            try:
                return {"uptime_sec": float(parts[0])}
            except ValueError:
                pass
    return {}


def parse_cpu_freq(text):
    """Parse scaling_cur_freq (kHz) into MHz."""
    for line in text.splitlines():
        line = line.strip()
        try:
            khz = int(line)
            return {"cpu_freq_mhz": khz // 1000}
        except ValueError:
            continue
    return {}


def parse_rtsp_count(text):
    """Parse netstat/grep output to count RTSP connections on port 554."""
    for line in text.splitlines():
        line = line.strip()
        try:
            return {"rtsp_clients": int(line)}
        except ValueError:
            continue
    return {}


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

    net_text = tn.cmd("cat /proc/net/dev", timeout=2)
    snap.update(parse_net_dev(net_text, prefix="net"))

    freq_text = tn.cmd(
        "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq 2>/dev/null",
        timeout=2,
    )
    snap.update(parse_cpu_freq(freq_text))

    uptime_text = tn.cmd("cat /proc/uptime", timeout=2)
    snap.update(parse_uptime(uptime_text))

    rtsp_text = tn.cmd(
        "netstat -tn 2>/dev/null | grep -c ':554 ' || echo 0",
        timeout=2,
    )
    snap.update(parse_rtsp_count(rtsp_text))

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


# ── Pi4 host monitoring ─────────────────────────────────────────────────────

PI4_THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
PI4_STAT_PATH = Path("/proc/stat")
PI4_MEMINFO_PATH = Path("/proc/meminfo")
PI4_NET_DEV_PATH = Path("/proc/net/dev")


def pi4_snapshot():
    """Collect Pi4 host metrics from local procfs/sysfs."""
    snap = {}

    # CPU temperature
    try:
        raw = PI4_THERMAL_PATH.read_text().strip()
        snap["pi4_cpu_temp_c"] = round(int(raw) / 1000.0, 1)
    except Exception:
        snap["pi4_cpu_temp_c"] = None

    # CPU jiffies (caller computes delta percentage)
    try:
        first_line = PI4_STAT_PATH.read_text().split("\n", 1)[0]
        snap.update(parse_stat(first_line + "\n"))
        # rename to pi4-specific keys so they don't collide with camera fields
        snap["pi4_cpu_total"] = snap.pop("cpu_total", None)
        snap["pi4_cpu_busy"] = snap.pop("cpu_busy", None)
    except Exception:
        snap["pi4_cpu_total"] = None
        snap["pi4_cpu_busy"] = None

    # Memory
    try:
        meminfo_text = PI4_MEMINFO_PATH.read_text()
        mem = parse_meminfo(meminfo_text, prefix="pi4_mem")
        snap["pi4_mem_total_kb"] = mem.get("pi4_mem_memtotal_kb")
        snap["pi4_mem_avail_kb"] = mem.get("pi4_mem_memavailable_kb")
    except Exception:
        snap["pi4_mem_total_kb"] = None
        snap["pi4_mem_avail_kb"] = None

    # Disk free space on the data/recordings volume
    try:
        st = os.statvfs("/app/data")
        snap["pi4_disk_free_mb"] = round((st.f_bavail * st.f_frsize) / (1024 * 1024), 1)
    except Exception:
        snap["pi4_disk_free_mb"] = None

    # Network RX stats (host side, complements camera TX)
    try:
        net_text = PI4_NET_DEV_PATH.read_text()
        # Try common interface names; eth0 is typical for Pi4 wired
        net = parse_net_dev(net_text, iface="eth0", prefix="pi4_net")
        if not net:
            net = parse_net_dev(net_text, iface="end0", prefix="pi4_net")
        snap["pi4_net_rx_bytes"] = net.get("pi4_net_rx_bytes")
        snap["pi4_net_rx_errors"] = net.get("pi4_net_rx_errors")
        snap["pi4_net_rx_dropped"] = net.get("pi4_net_rx_dropped")
    except Exception:
        snap["pi4_net_rx_bytes"] = None
        snap["pi4_net_rx_errors"] = None
        snap["pi4_net_rx_dropped"] = None

    return snap


def compute_pi4_cpu_percent(prev, curr):
    """Compute Pi4 CPU% between two snapshots."""
    pt = prev.get("pi4_cpu_total")
    pb = prev.get("pi4_cpu_busy")
    ct = curr.get("pi4_cpu_total")
    cb = curr.get("pi4_cpu_busy")
    if pt is None or ct is None or pb is None or cb is None:
        return None
    dt = ct - pt
    if dt <= 0:
        return None
    return round(100.0 * (cb - pb) / dt, 1)


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


def record_successful_login():
    """Persist the timestamp of the most recent confirmed-good login.

    Reads then writes settings.json so we only touch the
    ``last_successful_login`` field and don't trample any concurrent edits
    from the settings API.
    """
    try:
        ensure_dirs()
        saved = {}
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE) as f:
                    saved = json.load(f)
            except (json.JSONDecodeError, IOError):
                saved = {}
        saved["last_successful_login"] = datetime.now().isoformat()
        with open(SETTINGS_FILE, "w") as f:
            json.dump(saved, f, indent=2)
        logger.info("Recorded successful login at %s", saved["last_successful_login"])
    except Exception as exc:
        logger.warning("Could not save last_successful_login: %s", exc)


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
    samples_collected = 0

    try:
        with open(log_path, "w") as f:
            while not monitor_stop_event.is_set():
                snap = snapshot(tn)
                snap.update(fetch_isp_info(host))
                snap.update(pi4_snapshot())

                if prev_snap is not None:
                    cpu_pct = compute_cpu_percent(prev_snap, snap)
                    if cpu_pct is not None:
                        snap["cpu_percent"] = cpu_pct
                    pi4_pct = compute_pi4_cpu_percent(prev_snap, snap)
                    if pi4_pct is not None:
                        snap["pi4_cpu_percent"] = pi4_pct

                f.write(json.dumps(snap) + "\n")
                f.flush()

                ws_broadcast(snap)
                mavlink_broadcast(snap)

                with monitor_lock:
                    monitor_state["samples"] += 1
                    monitor_state["last_sample"] = snap

                samples_collected += 1
                if samples_collected == 1:
                    # First real sample proves credentials + commands work.
                    record_successful_login()

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
    safe = {
        **settings,
        "telnet_password": "••••••••" if settings.get("telnet_password") else "",
        "has_password": bool(settings.get("telnet_password")),
    }
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


# ── Monitor start helper (shared by HTTP + auto-start) ──────────────────────

def _start_monitor_thread() -> tuple[bool, str | None]:
    """Spawn the monitor thread if eligible.

    Returns (started, error_message). ``error_message`` is None on success.
    Caller is responsible for cancelling the auto-start loop if appropriate.
    """
    global monitor_thread, monitor_state

    with monitor_lock:
        if monitor_state["running"]:
            return False, "Already monitoring"

    settings = load_settings()
    if not settings.get("telnet_password"):
        return False, "Telnet password not configured. Set it in Settings first."

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
    return True, None


# ── Auto-start loop ─────────────────────────────────────────────────────────

def _cancel_auto_start(reason: str = "") -> None:
    """Signal the auto-start retry loop to exit, if it is running."""
    auto_start_stop_event.set()
    with auto_start_lock:
        if auto_start_state["active"]:
            logger.info("Auto-start: cancelling%s", f" ({reason})" if reason else "")
        auto_start_state["active"] = False


def _auto_start_loop() -> None:
    deadline = time.time() + AUTO_START_DURATION_SEC

    with auto_start_lock:
        auto_start_state.update({
            "active": True,
            "start_time": datetime.now().isoformat(),
            "deadline": datetime.fromtimestamp(deadline).isoformat(),
            "attempts": 0,
            "last_attempt_error": None,
        })

    logger.info("Auto-start: arming retries for %d seconds", AUTO_START_DURATION_SEC)

    try:
        while time.time() < deadline:
            if auto_start_stop_event.is_set():
                return

            with monitor_lock:
                already_running = monitor_state["running"]
            if already_running:
                logger.info("Auto-start: monitor already running; finishing")
                return

            with auto_start_lock:
                auto_start_state["attempts"] += 1
                attempt = auto_start_state["attempts"]
            logger.info("Auto-start: attempt #%d", attempt)

            started, err = _start_monitor_thread()
            if not started:
                with auto_start_lock:
                    auto_start_state["last_attempt_error"] = err
                logger.warning("Auto-start: could not start: %s", err)
            else:
                # Wait for the monitor to either fail (running goes False) or
                # produce its first sample (success).
                end = time.time() + AUTO_START_VERIFY_SEC
                samples = 0
                running = True
                err = None
                while time.time() < end:
                    if auto_start_stop_event.wait(0.5):
                        return
                    with monitor_lock:
                        running = monitor_state["running"]
                        samples = monitor_state["samples"]
                        err = monitor_state["error"]
                    if not running or samples > 0:
                        break

                if samples > 0:
                    logger.info("Auto-start: monitor producing samples; finishing")
                    return

                with auto_start_lock:
                    auto_start_state["last_attempt_error"] = err or "Monitor stopped before first sample"

            remaining = max(0.0, deadline - time.time())
            wait_time = min(AUTO_START_RETRY_INTERVAL_SEC, remaining)
            if wait_time <= 0:
                break
            if auto_start_stop_event.wait(wait_time):
                return
    finally:
        with auto_start_lock:
            auto_start_state["active"] = False
        logger.info("Auto-start: finished after %d attempt(s)", auto_start_state["attempts"])


def maybe_start_auto_start() -> None:
    """Kick off the auto-start retry loop if saved credentials are known good."""
    global auto_start_thread

    settings = load_settings()
    if not settings.get("telnet_password"):
        logger.info("Auto-start: no saved telnet password, skipping")
        return
    if not settings.get("last_successful_login"):
        logger.info("Auto-start: no prior successful login on record, skipping")
        return

    auto_start_stop_event.clear()
    auto_start_thread = threading.Thread(target=_auto_start_loop, daemon=True)
    auto_start_thread.start()


# ── API: Monitor control ────────────────────────────────────────────────────

@app.route("/api/start", methods=["POST"])
def start_monitor():
    _cancel_auto_start("manual start")

    started, err = _start_monitor_thread()
    if not started:
        status_code = 400 if err else 500
        return jsonify({"success": False, "message": err or "Failed to start"}), status_code

    return jsonify({"success": True})


@app.route("/api/stop", methods=["POST"])
def stop_monitor():
    _cancel_auto_start("manual stop")

    with monitor_lock:
        if not monitor_state["running"]:
            return jsonify({"success": True, "message": "Not running"})

    monitor_stop_event.set()

    if monitor_thread and monitor_thread.is_alive():
        monitor_thread.join(timeout=10)

    return jsonify({"success": True})


@app.route("/api/auto-start/cancel", methods=["POST"])
def cancel_auto_start_route():
    _cancel_auto_start("user cancelled")
    return jsonify({"success": True})


@app.route("/api/status", methods=["GET"])
def get_status():
    with monitor_lock:
        state = dict(monitor_state)
        if state.get("last_sample"):
            state["last_sample"] = dict(state["last_sample"])
    with auto_start_lock:
        state["auto_start"] = dict(auto_start_state)
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
    maybe_start_auto_start()
    logger.info("RadCam Spy starting on port 9850 (WS on 9851)")
    app.run(host="0.0.0.0", port=9850)
