import json
import os
import re
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

STREAMING_SCRIPT_URL = "https://raw.githubusercontent.com/xykt/IPQuality/main/ip.sh"
STREAMING_SCRIPT_PATH = Path("/tmp/ipquality_ip.sh")
STREAMING_SCRIPT_TTL = int(os.environ.get("STREAMING_SCRIPT_TTL", 60 * 60 * 12))

SCRIPT_SERVICE_MAP = {
    "Youtube": {"key": "youtube", "name": "YouTube"},
    "AmazonPrimeVideo": {"key": "prime_video", "name": "Prime Video"},
    "Netflix": {"key": "netflix", "name": "Netflix"},
    "DisneyPlus": {"key": "disney_plus", "name": "Disney+"},
    "HBO": {"key": "hbo", "name": "HBO"},
}


def _ensure_streaming_script() -> Path:
    """Download the upstream streaming test script if it is missing or stale."""

    if STREAMING_SCRIPT_PATH.exists():
        age = time.time() - STREAMING_SCRIPT_PATH.stat().st_mtime
        if age < STREAMING_SCRIPT_TTL:
            return STREAMING_SCRIPT_PATH

    response = requests.get(STREAMING_SCRIPT_URL, timeout=15)
    response.raise_for_status()

    STREAMING_SCRIPT_PATH.write_text(response.text)
    STREAMING_SCRIPT_PATH.chmod(0o755)
    return STREAMING_SCRIPT_PATH


def _clean_script_output(raw: str) -> str:
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)

    start = cleaned.find("{")
    if start != -1:
        cleaned = cleaned[start:]
    return cleaned


def _parse_streaming_results_from_script(raw: str) -> list[dict[str, Any]]:
    cleaned = _clean_script_output(raw)
    payload = json.loads(cleaned)
    media_entries = payload.get("Media") if isinstance(payload, dict) else None
    media = media_entries[0] if isinstance(media_entries, list) and media_entries else {}

    results: list[dict[str, Any]] = []
    for upstream_key, target in SCRIPT_SERVICE_MAP.items():
        entry = media.get(upstream_key) if isinstance(media, dict) else None
        status_text = str((entry or {}).get("Status") or "").lower()
        unlocked = any(token in status_text for token in ["解锁", "原生", "unblock", "yes", "open"])

        detail_parts = [
            (entry or {}).get("Status"),
            (entry or {}).get("Region"),
            (entry or {}).get("Type"),
        ]
        detail = " | ".join([part for part in detail_parts if part]) or None

        results.append(
            {
                "key": target["key"],
                "service": target["name"],
                "unlocked": unlocked,
                "status_code": None,
                "detail": detail or ("未返回结果" if entry is None else None),
            }
        )

    return results


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
    try:
        script_path = _ensure_streaming_script()
    except Exception as exc:  # pragma: no cover - network failures
        return jsonify(
            {
                "status": "error",
                "results": [],
                "detail": f"获取脚本失败: {exc}",
                "elapsed_ms": int((time.time() - start) * 1000),
            }
        ), 502

    try:
        proc = subprocess.run(
            ["bash", str(script_path), "-j", "-n"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return jsonify(
            {
                "status": "error",
                "results": [],
                "detail": "脚本执行超时",
                "elapsed_ms": int((time.time() - start) * 1000),
            }
        ), 504

    if proc.returncode != 0:
        return jsonify(
            {
                "status": "error",
                "results": [],
                "detail": proc.stderr.strip() or "执行失败",
                "elapsed_ms": int((time.time() - start) * 1000),
            }
        ), 500

    try:
        results = _parse_streaming_results_from_script(proc.stdout)
    except Exception as exc:  # pragma: no cover - defensive parsing
        return jsonify(
            {
                "status": "error",
                "results": [],
                "detail": f"解析结果失败: {exc}",
                "elapsed_ms": int((time.time() - start) * 1000),
            }
        ), 500

    elapsed_ms = int((time.time() - start) * 1000)
    return jsonify({"status": "ok", "results": results, "elapsed_ms": elapsed_ms})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=AGENT_API_PORT)
