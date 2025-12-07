import json
import os
import re
import shlex
import socket
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
# Streaming probe mode: "builtin" (fast HTTP reachability checks) or "external" (ip.sh).
# The builtin mode is the default to avoid long-running external scripts.
STREAMING_PROBE_MODE = os.environ.get("STREAMING_PROBE_MODE", "builtin").lower()
STREAMING_HTTP_TIMEOUT = float(os.environ.get("STREAMING_HTTP_TIMEOUT", 5))
STREAMING_CACHE_PATH = Path(os.environ.get("STREAMING_CACHE_PATH", "/tmp/streaming_probe_cache.json"))
STREAMING_AUTO_INTERVAL = int(os.environ.get("STREAMING_AUTO_INTERVAL", 60 * 60 * 24))
STREAMING_AUTO_ENABLED = os.environ.get("STREAMING_AUTO_ENABLED", "true").lower() != "false"

SCRIPT_SERVICE_MAP = {
    "Youtube": {"key": "youtube", "name": "YouTube"},
    "AmazonPrimeVideo": {"key": "prime_video", "name": "Prime Video"},
    "Netflix": {"key": "netflix", "name": "Netflix"},
    "DisneyPlus": {"key": "disney_plus", "name": "Disney+"},
    "HBO": {"key": "hbo", "name": "HBO"},
}

STREAMING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _resolve_hostname(hostname: str) -> tuple[str | None, list[str]]:
    """Return the preferred nameserver and resolved IP list for a hostname."""

    nameserver: str | None = None
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("nameserver"):
                    nameserver = line.split()[1]
                    break
    except FileNotFoundError:
        nameserver = None

    ips: list[str] = []
    try:
        infos = socket.getaddrinfo(hostname, None)
        for info in infos:
            addr = info[4][0]
            if addr not in ips:
                ips.append(addr)
    except socket.gaierror:
        pass

    if not ips:
        try:
            proc = subprocess.run(
                ["dig", "+short", hostname], capture_output=True, text=True, timeout=5
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if line and re.match(r"^\d+\.\d+\.\d+\.\d+$", line):
                        ips.append(line)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return nameserver, ips


def _dns_detail(hostname: str) -> str:
    nameserver, ips = _resolve_hostname(hostname)
    nameserver_display = nameserver or "unknown"
    ip_display = ", ".join(ips[:3]) if ips else "unresolved"
    return f"DNS {nameserver_display} -> {ip_display}"


def _service_result(
    key: str,
    service: str,
    unlocked: bool,
    status_code: int | None,
    detail_parts: list[str | None],
) -> dict[str, Any]:
    detail = " | ".join([part for part in detail_parts if part]) or None
    return {
        "key": key,
        "service": service,
        "unlocked": unlocked,
        "status_code": status_code,
        "detail": detail,
    }


def _probe_tiktok() -> dict[str, Any]:
    url = "https://www.tiktok.com/"
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("www.tiktok.com")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        status = resp.status_code
        region_match = re.search(r'"region"\s*:\s*"([A-Z]{2})"', resp.text)
        region = region_match.group(1) if region_match else None
        unlocked = status == 200 and region is not None
        detail_parts = [dns_info, f"HTTP {status}", f"Region: {region}" if region else None]
    except requests.RequestException as exc:
        unlocked = False
        status = None
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    return _service_result("tiktok", "TikTok", unlocked, status, detail_parts)


def _probe_disney_plus() -> dict[str, Any]:
    url = "https://www.disneyplus.com/"
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("www.disneyplus.com")
    try:
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        status = resp.status_code
        unlocked = status == 200 and "not available in your region" not in resp.text.lower()
        region_header = resp.headers.get("X-Region") or resp.headers.get("CF-IPCountry")
        detail_parts = [dns_info, f"HTTP {status}", f"Region: {region_header}" if region_header else None]
    except requests.RequestException as exc:
        unlocked = False
        status = None
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    return _service_result("disney_plus", "Disney+", unlocked, status, detail_parts)


def _probe_netflix() -> dict[str, Any]:
    test_title = "https://www.netflix.com/title/80018499"
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("www.netflix.com")
    try:
        resp = requests.get(test_title, headers=headers, timeout=10)
        status = resp.status_code
        blocked_text = "not available to watch" in resp.text.lower() or "proxy" in resp.text.lower()
        unlocked = status == 200 and not blocked_text
        detail_parts = [dns_info, f"HTTP {status}", "自制片库" if status == 404 else None]
    except requests.RequestException as exc:
        unlocked = False
        status = None
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    return _service_result("netflix", "Netflix", unlocked, status, detail_parts)


def _probe_youtube_premium() -> dict[str, Any]:
    url = "https://www.youtube.com/premium"
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("www.youtube.com")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        status = resp.status_code
        region_match = re.search(r'"countryCode"\s*:\s*"([A-Z]{2})"', resp.text)
        region_match = region_match or re.search(r'"contentRegion"\s*:\s*"([A-Z]{2})"', resp.text)
        region = region_match.group(1) if region_match else None
        unavailable = "not available in your country" in resp.text.lower()
        unlocked = status == 200 and not unavailable
        detail_parts = [dns_info, f"HTTP {status}", f"Region: {region}" if region else None]
    except requests.RequestException as exc:
        unlocked = False
        status = None
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    return _service_result("youtube_premium", "YouTube Premium", unlocked, status, detail_parts)


def _probe_prime_video() -> dict[str, Any]:
    url = "https://www.primevideo.com/"
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("www.primevideo.com")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        status = resp.status_code
        region_match = re.search(r'"currentTerritory"\s*:\s*"([A-Z]{2})"', resp.text)
        region = region_match.group(1) if region_match else None
        unlocked = status == 200 and region is not None
        detail_parts = [dns_info, f"HTTP {status}", f"Region: {region}" if region else None]
    except requests.RequestException as exc:
        unlocked = False
        status = None
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    return _service_result("prime_video", "Prime Video", unlocked, status, detail_parts)


def _probe_spotify() -> dict[str, Any]:
    url = "https://www.spotify.com/api/ip-address"
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("www.spotify.com")
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        status = resp.status_code
        country = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            payload = resp.json()
            country = payload.get("country") or payload.get("ip_country")
        unlocked = status == 200 and country is not None
        detail_parts = [dns_info, f"HTTP {status}", f"Country: {country}" if country else None]
    except requests.RequestException as exc:
        unlocked = False
        status = None
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    return _service_result("spotify", "Spotify", unlocked, status, detail_parts)


def _probe_openai() -> dict[str, Any]:
    dns_info = _dns_detail("api.openai.com")
    compliance_url = "https://api.openai.com/compliance/cookie_requirements"
    trace_url = "https://chat.openai.com/cdn-cgi/trace"
    headers = {"User-Agent": STREAMING_UA}

    unsupported_country = None
    api_status: int | None = None
    trace_loc: str | None = None
    api_error: str | None = None

    try:
        resp = requests.get(compliance_url, headers=headers, timeout=10)
        api_status = resp.status_code
        if resp.headers.get("content-type", "").startswith("application/json"):
            payload = resp.json()
            unsupported_country = payload.get("unsupported_country")
    except requests.RequestException as exc:
        api_error = f"API 请求失败: {exc}"[:150]

    try:
        trace_resp = requests.get(trace_url, headers=headers, timeout=10)
        if trace_resp.status_code == 200:
            for line in trace_resp.text.splitlines():
                if line.startswith("loc="):
                    trace_loc = line.split("=", 1)[1].strip()
                    break
    except requests.RequestException:
        pass

    unlocked = api_status == 200 and not unsupported_country
    detail_parts = [
        dns_info,
        f"API HTTP {api_status}" if api_status else api_error,
        f"Unsupported: {unsupported_country}" if unsupported_country else None,
        f"loc={trace_loc}" if trace_loc else None,
    ]
    return _service_result("openai", "OpenAI/ChatGPT", unlocked, api_status, detail_parts)


def _run_streaming_suite() -> tuple[list[dict[str, Any]], int]:
    start = time.time()
    checks = [
        _probe_tiktok,
        _probe_disney_plus,
        _probe_netflix,
        _probe_youtube_premium,
        _probe_prime_video,
        _probe_spotify,
        _probe_openai,
    ]

    results: list[dict[str, Any]] = []
    for fn in checks:
        try:
            results.append(fn())
        except Exception as exc:  # pragma: no cover - defensive
            results.append(
                {
                    "service": getattr(fn, "__name__", "unknown"),
                    "unlocked": False,
                    "detail": f"内部错误: {exc}"[:150],
                    "status_code": None,
                    "key": getattr(fn, "__name__", "unknown"),
                }
            )

    elapsed_ms = int((time.time() - start) * 1000)
    return results, elapsed_ms


def _load_cached_probe(max_age: int | None = None) -> dict[str, Any] | None:
    if not STREAMING_CACHE_PATH.exists():
        return None

    try:
        payload = json.loads(STREAMING_CACHE_PATH.read_text())
    except Exception:
        return None

    if max_age is not None:
        ts = payload.get("timestamp")
        if not ts or (time.time() - ts) > max_age:
            return None

    return payload


def _save_cached_probe(payload: dict[str, Any]) -> None:
    payload["timestamp"] = int(time.time())
    STREAMING_CACHE_PATH.write_text(json.dumps(payload))


def _run_and_cache_probe() -> dict[str, Any]:
    results, elapsed_ms = _run_streaming_suite()
    payload = {"status": "ok", "results": results, "elapsed_ms": elapsed_ms}
    _save_cached_probe(payload)
    return payload


class StreamingAutoRunner(threading.Thread):
    def __init__(self, interval_seconds: int) -> None:
        super().__init__(daemon=True)
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        # Run immediately on startup, then on the configured interval.
        try:
            _run_and_cache_probe()
        except Exception:
            pass

        while not self._stop_event.wait(self.interval_seconds):
            try:
                _run_and_cache_probe()
            except Exception:
                continue

    def stop(self) -> None:
        self._stop_event.set()


auto_runner: StreamingAutoRunner | None = None
if STREAMING_AUTO_ENABLED:
    auto_runner = StreamingAutoRunner(STREAMING_AUTO_INTERVAL)
    auto_runner.start()


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


def _probe_streaming_targets_http() -> list[dict[str, Any]]:
    """
    Lightweight reachability checks for major streaming services.

    We only care about fast feedback (is the service reachable, any geo/ban headers),
    not the full unlock heuristics from the upstream ip.sh script. Each request uses a
    short timeout to keep the overall probe bounded and predictable.
    """

    results: list[dict[str, Any]] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }

    for key, target in STREAMING_TARGETS.items():
        url = target["url"]
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=STREAMING_HTTP_TIMEOUT,
                allow_redirects=True,
            )
            status_code = resp.status_code
            unlocked = status_code < 400
            region = resp.headers.get("X-Region") or resp.headers.get("CF-IPCountry")
            detail_parts = [f"HTTP {status_code}", resp.reason]
            if region:
                detail_parts.append(f"Region: {region}")
            detail = " | ".join([p for p in detail_parts if p])
        except requests.Timeout:
            unlocked = False
            status_code = None
            detail = "请求超时"
        except requests.RequestException as exc:
            unlocked = False
            status_code = None
            detail = f"请求失败: {exc}"[:120]

        results.append(
            {
                "key": key,
                "service": target["name"],
                "unlocked": unlocked,
                "status_code": status_code,
                "detail": detail,
            }
        )

    return results


def _probe_streaming_targets_full() -> dict[str, Any]:
    cached = _load_cached_probe(max_age=STREAMING_AUTO_INTERVAL // 2)
    if cached:
        return cached

    return _run_and_cache_probe()


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
    refresh = request.args.get("refresh", "false").lower() in ["1", "true", "yes"]

    if STREAMING_PROBE_MODE == "external":
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
        payload = {"status": "ok", "results": results, "elapsed_ms": elapsed_ms}
        _save_cached_probe(payload)
        return jsonify(payload)

    if refresh:
        payload = _run_and_cache_probe()
    else:
        payload = _probe_streaming_targets_full()

    payload.setdefault("elapsed_ms", int((time.time() - start) * 1000))
    payload["status"] = payload.get("status") or "ok"
    return jsonify(payload)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=AGENT_API_PORT)
