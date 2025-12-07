import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

DEFAULT_IPERF_PORT = int(Path("/app").joinpath("IPERF_PORT").read_text().strip()) if Path("/app/IPERF_PORT").exists() else 5201
AGENT_API_PORT = int(os.environ.get("AGENT_API_PORT", "8000"))

server_process: subprocess.Popen | None = None
server_lock = threading.Lock()

STREAMING_TARGETS = {
    "youtube": {"name": "YouTube", "url": "https://www.youtube.com"},
    "prime_video": {"name": "Prime Video", "url": "https://www.primevideo.com"},
    "netflix": {"name": "Netflix", "url": "https://www.netflix.com"},
    "disney_plus": {"name": "Disney+", "url": "https://www.disneyplus.com"},
    "hbo": {"name": "HBO", "url": "https://www.hbomax.com"},
}


def _read_port_from_request(data: Dict[str, Any]) -> int:
    port = int(data.get("port", DEFAULT_IPERF_PORT))
    if port <= 0 or port > 65535:
        raise ValueError("invalid port")
    return port


def _start_server_process(port: int) -> subprocess.Popen:
    cmd = f"iperf3 -s -p {port}"
    return subprocess.Popen(shlex.split(cmd))


def _is_process_running(proc: subprocess.Popen | None) -> bool:
    return proc is not None and proc.poll() is None


@app.route("/health", methods=["GET"])
def health() -> Any:
    return jsonify({
        "status": "ok",
        "server_running": _is_process_running(server_process),
        "port": DEFAULT_IPERF_PORT,
        "timestamp": int(time.time())
    })


@app.route("/start_server", methods=["POST"])
def start_server() -> Any:
    global server_process, DEFAULT_IPERF_PORT
    data = request.get_json(silent=True) or {}
    try:
        requested_port = _read_port_from_request(data)
    except ValueError:
        return jsonify({"status": "error", "error": "invalid_port"}), 400

    with server_lock:
        if _is_process_running(server_process):
            return jsonify({"status": "running", "port": requested_port})

        DEFAULT_IPERF_PORT = requested_port
        server_process = _start_server_process(DEFAULT_IPERF_PORT)
        return jsonify({"status": "started", "port": DEFAULT_IPERF_PORT})


@app.route("/stop_server", methods=["POST"])
def stop_server() -> Any:
    global server_process
    with server_lock:
        if _is_process_running(server_process):
            server_process.terminate()
            server_process = None
            return jsonify({"status": "stopped"})
        return jsonify({"status": "not_running"})


@app.route("/run_test", methods=["POST"])
def run_test() -> Any:
    data = request.get_json(force=True)
    target = data.get("target")
    if not target:
        return jsonify({"status": "error", "error": "missing_target"}), 400

    try:
        port = _read_port_from_request(data)
        duration = int(data.get("duration", 10))
        protocol = data.get("protocol", "tcp")
        parallel = int(data.get("parallel", 1))
    except ValueError:
        return jsonify({"status": "error", "error": "invalid_parameter"}), 400

    proto_flag = "-u" if protocol.lower() == "udp" else ""
    cmd = f"iperf3 -c {target} -p {port} -t {duration} -P {parallel} {proto_flag} -J"

    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=duration + 15,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "error": "timeout"}), 504

    if result.returncode != 0:
        return jsonify({"status": "error", "error": result.stderr.strip()}), 500

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        return jsonify({"status": "error", "error": "parse_failed"}), 500

    return jsonify({"status": "ok", "iperf_result": output})


@app.route("/streaming_probe", methods=["GET"])
def streaming_probe() -> Any:
    start = time.time()
    results = []
    for key, target in STREAMING_TARGETS.items():
        url = target.get("url")
        name = target.get("name", key)
        unlocked = False
        status_code: int | None = None
        detail: str | None = None
        try:
            response = requests.get(url, timeout=5)
            status_code = response.status_code
            unlocked = 200 <= response.status_code < 400
            detail = f"HTTP {response.status_code}"
        except requests.RequestException as exc:  # pragma: no cover - network failures
            detail = str(exc)

        results.append(
            {
                "key": key,
                "service": name,
                "unlocked": unlocked,
                "status_code": status_code,
                "detail": detail,
            }
        )

    elapsed_ms = int((time.time() - start) * 1000)
    return jsonify({"status": "ok", "results": results, "elapsed_ms": elapsed_ms})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=AGENT_API_PORT)
