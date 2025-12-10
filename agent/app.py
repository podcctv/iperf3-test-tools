import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from flask import Flask, jsonify, request, abort

# Import IP whitelist module
from ip_whitelist import whitelist

app = Flask(__name__)

DEFAULT_IPERF_PORT = int(Path("/app").joinpath("IPERF_PORT").read_text().strip()) if Path("/app/IPERF_PORT").exists() else 62001
AGENT_API_PORT = int(os.environ.get("AGENT_API_PORT", "8000"))
AGENT_VERSION = "1.0.2"  # Update this when releasing new agent versions

# Reverse mode configuration for internal agents behind NAT
AGENT_MODE = os.environ.get("AGENT_MODE", "normal").lower()
MASTER_URL = os.environ.get("MASTER_URL", "")
NODE_NAME = os.environ.get("NODE_NAME", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
IPERF_PORT = int(os.environ.get("IPERF_PORT", str(DEFAULT_IPERF_PORT)))

# Try to load from config.json if env vars not set
_config_path = Path("/app/config.json")
if _config_path.exists():
    try:
        _config = json.loads(_config_path.read_text())
        if not MASTER_URL:
            MASTER_URL = _config.get("master_url", "")
        if not NODE_NAME:
            NODE_NAME = _config.get("node_name", "")
        if AGENT_MODE == "normal":
            AGENT_MODE = _config.get("mode", "normal")
        POLL_INTERVAL = _config.get("poll_interval", POLL_INTERVAL)
        IPERF_PORT = _config.get("iperf_port", IPERF_PORT)
    except Exception:
        pass

_reverse_thread: threading.Thread | None = None
_reverse_running = False

server_process: subprocess.Popen | None = None
server_lock = threading.Lock()

STREAMING_TARGETS = {
    "youtube": {"name": "YouTube", "url": "https://www.youtube.com"},
    "prime_video": {"name": "Prime Video", "url": "https://www.primevideo.com"},
    "netflix": {"name": "Netflix", "url": "https://www.netflix.com"},
    "disney_plus": {"name": "Disney+", "url": "https://www.disneyplus.com"},
    "hbo": {"name": "HBO", "url": "https://www.hbomax.com"},
    "gemini": {"name": "Google Gemini", "url": "https://gemini.google.com/app"},
}

STREAMING_SCRIPT_URL = "https://raw.githubusercontent.com/1-stream/RegionRestrictionCheck/main/check.sh"
STREAMING_SCRIPT_PATH = Path("/tmp/region_restriction_check.sh")
STREAMING_SCRIPT_TTL = int(os.environ.get("STREAMING_SCRIPT_TTL", 60 * 60 * 12))
# Streaming probe mode: "builtin" (fast HTTP reachability checks) or "external" (ip.sh).
# The builtin mode is the default to avoid long-running external scripts.
STREAMING_PROBE_MODE = os.environ.get("STREAMING_PROBE_MODE", "builtin").lower()
STREAMING_HTTP_TIMEOUT = float(os.environ.get("STREAMING_HTTP_TIMEOUT", 5))
STREAMING_CACHE_PATH = Path(os.environ.get("STREAMING_CACHE_PATH", "/tmp/streaming_probe_cache.json"))
STREAMING_AUTO_INTERVAL = int(os.environ.get("STREAMING_AUTO_INTERVAL", 60 * 60 * 24))
STREAMING_AUTO_ENABLED = os.environ.get("STREAMING_AUTO_ENABLED", "true").lower() != "false"

BACKBONE_TARGETS = [
    {
        "key": "zj_cu",
        "name": "浙江联通",
        "host": "zj-cu-v4.ip.zstaticcdn.com",
        "port": 443,
    },
    {
        "key": "zj_cm",
        "name": "浙江移动",
        "host": "zj-cm-v4.ip.zstaticcdn.com",
        "port": 443,
    },
    {
        "key": "zj_ct",
        "name": "浙江电信",
        "host": "zj-ct-v4.ip.zstaticcdn.com",
        "port": 443,
    },
]
BACKBONE_LATENCY_TTL = int(os.environ.get("BACKBONE_LATENCY_TTL", 60))
_backbone_cache: dict[str, Any] = {"timestamp": 0, "results": []}

SCRIPT_SERVICE_MAP = {
    "Youtube": {"key": "youtube", "name": "YouTube"},
    "AmazonPrimeVideo": {"key": "prime_video", "name": "Prime Video"},
    "Netflix": {"key": "netflix", "name": "Netflix"},
    "DisneyPlus": {"key": "disney_plus", "name": "Disney+"},
    "HBO": {"key": "hbo", "name": "HBO"},
    "TikTok": {"key": "tiktok", "name": "TikTok"},
    "Spotify": {"key": "spotify", "name": "Spotify"},
    "OpenAI": {"key": "openai", "name": "OpenAI/ChatGPT"},
    "Gemini": {"key": "gemini", "name": "Google Gemini"},
    "Twitch": {"key": "twitch", "name": "Twitch"},
    "ParamountPlus": {"key": "paramount_plus", "name": "Paramount+"},
    "Bilibili": {"key": "bilibili", "name": "Bilibili"},
    "AppleMusic": {"key": "apple_music", "name": "Apple Music"},
    "Telegram": {"key": "telegram", "name": "Telegram"},
    "WhatsApp": {"key": "whatsapp", "name": "WhatsApp"},
}

STREAMING_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

YOUTUBE_PREMIUM_COOKIES = (
    "YSC=BiCUU3-5Gdk; CONSENT=YES+cb.20220301-11-p0.en+FX+700; GPS=1; "
    "VISITOR_INFO1_LIVE=4VwPMkB7W5A; PREF=tz=Asia.Shanghai; _gcl_au=1.1."
    "1809531354.1646633279"
)

_media_cookie_cache: str | None = None


def _get_media_cookie() -> str | None:
    global _media_cookie_cache
    if _media_cookie_cache:
        return _media_cookie_cache

    try:
        resp = requests.get(
            "https://raw.githubusercontent.com/1-stream/RegionRestrictionCheck/main/cookies",
            timeout=10,
        )
        if resp.ok:
            _media_cookie_cache = resp.text
    except requests.RequestException:
        return None

    return _media_cookie_cache


def _extract_region_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/([A-Za-z]{2})(?:[/?#]|$)", url)
    code = match.group(1) if match else None
    return code.upper() if code else None


def _measure_backbone_target(target: dict) -> Dict[str, Any]:
    samples: list[float] = []
    detail: str | None = None
    for _ in range(2):
        start = time.perf_counter()
        try:
            with socket.create_connection(
                (target["host"], int(target["port"])), timeout=3
            ):
                pass
            samples.append((time.perf_counter() - start) * 1000)
        except OSError as exc:
            detail = str(exc)

    latency_ms = round(sum(samples) / len(samples), 2) if samples else None
    return {
        "key": target.get("key"),
        "name": target.get("name"),
        "host": target.get("host"),
        "port": int(target.get("port", 0)),
        "latency_ms": latency_ms,
        "status": "ok" if latency_ms is not None else "error",
        "detail": None if latency_ms is not None else detail,
        "checked_at": int(time.time()),
    }


def _get_backbone_latency() -> List[Dict[str, Any]]:
    now = time.time()
    if _backbone_cache["results"] and now - _backbone_cache["timestamp"] < BACKBONE_LATENCY_TTL:
        return _backbone_cache["results"]

    results = [_measure_backbone_target(target) for target in BACKBONE_TARGETS]
    _backbone_cache["timestamp"] = now
    _backbone_cache["results"] = results
    return results


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
    *,
    tier: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    detail = " | ".join([part for part in detail_parts if part]) or None
    result = {
        "key": key,
        "service": service,
        "unlocked": unlocked,
        "status_code": status_code,
        "detail": detail,
    }
    if tier:
        result["tier"] = tier
    if region:
        result["region"] = region
    return result


def _probe_tiktok() -> dict[str, Any]:
    dns_info = _dns_detail("www.tiktok.com")
    headers = {"User-Agent": STREAMING_UA}
    try:
        resp = requests.get(
            "https://www.tiktok.com/",
            headers=headers,
            timeout=10,
            allow_redirects=True,
        )
        region_resp = requests.post(
            "https://www.tiktok.com/passport/web/store_region/",
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]
        return _service_result("tiktok", "TikTok", False, None, detail_parts)

    status = resp.status_code
    region = None
    try:
        payload = region_resp.json()
        region = payload.get("data", {}).get("store_region")
    except Exception:
        region = None

    redirected_path = resp.url or ""
    if any(token in redirected_path for token in ["about", "status", "landing"]):
        unlocked = False
        detail = f"Region: {region}" if region else "未解锁"
        if region and region.lower() == "cn":
            detail = "由抖音提供"
    else:
        unlocked = True
        detail = f"Region: {region}" if region else "已解锁"

    detail_parts = [dns_info, f"HTTP {status}", detail]
    return _service_result("tiktok", "TikTok", unlocked, status, detail_parts)


def _probe_disney_plus() -> dict[str, Any]:
    dns_info = _dns_detail("www.disneyplus.com")
    cookies_blob = _get_media_cookie()
    if not cookies_blob:
        return _service_result(
            "disney_plus", "Disney+", False, None, [dns_info, "获取认证信息失败"]
        )

    cookie_lines = cookies_blob.splitlines()
    pre_cookie = cookie_lines[0] if len(cookie_lines) >= 1 else None
    fake_cookie = cookie_lines[7] if len(cookie_lines) >= 8 else None

    if not pre_cookie or not fake_cookie:
        return _service_result(
            "disney_plus", "Disney+", False, None, [dns_info, "认证模板缺失"]
        )

    try:
        pre_assertion = requests.post(
            "https://disney.api.edge.bamgrid.com/devices",
            headers={
                "authorization": "Bearer ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84",
                "content-type": "application/json; charset=UTF-8",
                "User-Agent": STREAMING_UA,
            },
            json={"deviceFamily": "browser", "applicationRuntime": "chrome", "deviceProfile": "windows", "attributes": {}},
            timeout=10,
        )
        assertion = pre_assertion.json().get("assertion")
    except Exception as exc:  # pragma: no cover - network
        detail_parts = [dns_info, f"预检失败: {exc}"[:150]]
        return _service_result("disney_plus", "Disney+", False, None, detail_parts)

    disney_cookie = pre_cookie.replace("DISNEYASSERTION", assertion)
    try:
        token_resp = requests.post(
            "https://disney.api.edge.bamgrid.com/token",
            headers={
                "authorization": "Bearer ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84",
                "User-Agent": STREAMING_UA,
            },
            data=disney_cookie,
            timeout=10,
        )
    except requests.RequestException as exc:
        detail_parts = [dns_info, f"Token 获取失败: {exc}"[:150]]
        return _service_result("disney_plus", "Disney+", False, None, detail_parts)

    if "forbidden-location" in token_resp.text or token_resp.status_code == 403:
        detail_parts = [dns_info, "地区封禁"]
        return _service_result("disney_plus", "Disney+", False, token_resp.status_code, detail_parts)

    try:
        refresh_token = token_resp.json().get("refresh_token")
    except Exception:
        refresh_token = None

    disney_payload = fake_cookie.replace("ILOVEDISNEY", refresh_token or "")
    try:
        graph_resp = requests.post(
            "https://disney.api.edge.bamgrid.com/graph/v1/device/graphql",
            headers={
                "authorization": "ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84",
                "User-Agent": STREAMING_UA,
            },
            data=disney_payload,
            timeout=10,
        )
        graph_json = graph_resp.json()
    except Exception as exc:  # pragma: no cover - network
        detail_parts = [dns_info, f"GraphQL 失败: {exc}"[:150]]
        return _service_result("disney_plus", "Disney+", False, None, detail_parts)

    country_code = None
    in_supported_location = None
    try:
        gql_data = graph_json.get("extensions", {}).get("sdk", {}).get("session", {}).get("location", {})
        country_code = gql_data.get("countryCode")
        in_supported_location = gql_data.get("inSupportedLocation")
    except Exception:
        pass

    try:
        preview_url = requests.get("https://www.disneyplus.com", timeout=10).url
    except requests.RequestException:
        preview_url = ""
    is_unavailable = "unavailable" in preview_url

    detail_parts = [dns_info]
    if country_code:
        detail_parts.append(f"Region: {country_code}")

    if country_code == "JP":
        unlocked = True
        status_text = "可用 (JP)"
    elif in_supported_location is False and not is_unavailable:
        unlocked = False
        status_text = f"即将上线 {country_code}" if country_code else "即将上线"
    elif is_unavailable:
        unlocked = False
        status_text = "未开放"
    elif in_supported_location:
        unlocked = True
        status_text = f"可用 (Region: {country_code})" if country_code else "可用"
    else:
        unlocked = False
        status_text = "未知"

    detail_parts.append(status_text)
    return _service_result(
        "disney_plus",
        "Disney+",
        unlocked,
        graph_resp.status_code,
        detail_parts,
        region=country_code,
    )



def _probe_netflix() -> dict[str, Any]:
    dns_info = _dns_detail("www.netflix.com")
    
    # 81280792: "Alive" (Korean Movie) - reasonably global
    # 70143836: "Breaking Bad" - widely available but region locked in some
    # 80018294: "Rick and Morty" - often region locked
    title_urls = [
        ("81280792", "originals"), # Should cover Originals
        ("70143836", "licensed"),  # Should cover Licensed
    ]

    results = {}
    region = None
    
    for title_id, kind in title_urls:
        url = f"https://www.netflix.com/title/{title_id}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": STREAMING_UA},
                timeout=10,
                allow_redirects=True,
            )
        except requests.RequestException:
            results[kind] = False
            continue

        if resp.status_code == 404:
            results[kind] = False
            continue
            
        # Try to find region
        if not region:
            # Look for requestCountry in standard JSON blobs
            match = re.search(r'"requestCountry"\s*:\s*"([A-Z]{2})"', resp.text)
            if match:
                region = match.group(1)

        # Check availability text
        # "Title Not Available" or similar text usually indicates lock
        # Redirect to /browse usually means title not found in region
        
        is_available = True
        if "/browse" in resp.url or "/signup" in resp.url:
            is_available = False
        elif "Not Available" in resp.text or "Lost in Space" in resp.text: # "Lost in Space" is the 404 page text sometimes
             is_available = False
        
        results[kind] = is_available

    originals = results.get("originals", False)
    licensed = results.get("licensed", False)
    
    if originals and licensed:
        tier = "full"
        unlocked = True
        detail = "全解锁"
    elif originals:
        tier = "originals"
        unlocked = False
        detail = "仅解锁自制剧"
    else:
        tier = "none"
        unlocked = False
        detail = "未解锁"

    detail_parts = [dns_info, detail]
    if region:
        detail_parts.append(f"Region: {region}")

    if not any(results.values()) and not region:
         # If everything failed, check if we got a 403 or network error implicit in results
         pass

    return _service_result(
        "netflix",
        "Netflix",
        unlocked,
        200 if originals or licensed else 403,
        detail_parts,
        tier=tier,
        region=region,
    )


def _probe_youtube_premium() -> dict[str, Any]:
    url = "https://www.youtube.com/premium"
    headers = {
        "User-Agent": STREAMING_UA,
        "Accept-Language": "en",
        "Cookie": YOUTUBE_PREMIUM_COOKIES,
    }
    dns_info = _dns_detail("www.youtube.com")
    try:
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
    except requests.RequestException as exc:
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]
        return _service_result("youtube", "YouTube Premium", False, None, detail_parts)

    status = resp.status_code
    region_match = re.search(r'"countryCode"\s*:\s*"([A-Z]{2})"', resp.text)
    region_match = region_match or re.search(r'"contentRegion"\s*:\s*"([A-Z]{2})"', resp.text)
    region = region_match.group(1) if region_match else None
    is_cn = (
        "www.google.cn" in resp.text
        or "www.google.cn" in resp.url
        or (region and region.upper() == "CN")
    )
    available = any(
        token in resp.text for token in ["purchaseButtonOverride", "Start trial"]
    ) or bool(region)

    if is_cn:
        unlocked = False
        detail = "CN 受限"
    elif available:
        unlocked = True
        detail = f"Region: {region}" if region else "已解锁"
    else:
        unlocked = False
        detail = f"Region: {region}" if region else "未解锁"

    detail_parts = [dns_info, f"HTTP {status}", detail]
    return _service_result(
        "youtube",
        "YouTube Premium",
        unlocked,
        status,
        detail_parts,
        region=region,
    )


def _probe_prime_video() -> dict[str, Any]:
    dns_info = _dns_detail("www.primevideo.com")
    try:
        home = requests.get(
            "https://www.primevideo.com",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]
        return _service_result("prime_video", "Prime Video", False, None, detail_parts)

    status = home.status_code
    region_match = re.search(r'"currentTerritory"\s*:\s*"([A-Z]{2})"', home.text)
    region = region_match.group(1) if region_match else None
    vpn_block = "VPN" in home.text or "proxy" in home.text

    detail_parts = [dns_info, f"HTTP {status}", f"Region: {region}" if region else None]
    if vpn_block:
        detail_parts.append("检测到代理/VPN")

    unlocked = region is not None and not vpn_block
    return _service_result(
        "prime_video",
        "Prime Video",
        unlocked,
        status,
        detail_parts,
        region=region,
    )


def _probe_spotify() -> dict[str, Any]:
    dns_info = _dns_detail("www.spotify.com")
    try:
        resp = requests.get(
            "https://www.spotify.com/tw/signup",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
        )
    except requests.RequestException as exc:
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]
        return _service_result("spotify", "Spotify", False, None, detail_parts)

    status = resp.status_code
    country_match = re.search(r'geoCountry":"([A-Z]{2})"', resp.text)
    country = country_match.group(1) if country_match else None
    unlocked = country is not None
    detail_parts = [dns_info, f"HTTP {status}", f"Region: {country}" if country else None]
    return _service_result(
        "spotify", "Spotify", unlocked, status, detail_parts, region=country
    )


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
    return _service_result(
        "openai",
        "OpenAI/ChatGPT",
        unlocked,
        api_status,
        detail_parts,
        region=trace_loc,
    )


def _probe_gemini() -> dict[str, Any]:
    headers = {"User-Agent": STREAMING_UA}
    dns_info = _dns_detail("gemini.google.com")
    url = "https://gemini.google.com/app"
    api_probe_status: int | None = None
    api_probe_error: str | None = None
    api_geo_hint: str | None = None
    trace_loc: str | None = None

    try:
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        status = resp.status_code
        blocked = "not available in your country" in resp.text.lower()
        blocked = blocked or "not available in your location" in resp.text.lower()
        unlocked = status and status < 400 and not blocked
        detail_parts = [
            dns_info,
            f"HTTP {status}",
            resp.url if resp.url and resp.url != url else None,
        ]
    except requests.RequestException as exc:
        status = None
        unlocked = False
        detail_parts = [dns_info, f"请求失败: {exc}"[:150]]

    # Attempt an unauthenticated API reachability check for a stronger signal.
    if status is None or not unlocked:
        api_url = "https://generativelanguage.googleapis.com/v1beta/models"
        try:
            api_resp = requests.get(api_url, headers=headers, timeout=10)
            api_probe_status = api_resp.status_code
            if api_resp.headers.get("content-type", "").startswith("application/json"):
                payload = api_resp.json()
                api_geo_hint = (
                    payload.get("error", {})
                    .get("message", "")
                    .split(".")[0]
                    if isinstance(payload, dict)
                    else None
                )
            unlocked = unlocked or (api_probe_status in {400, 401, 403})
        except requests.RequestException as exc:
            api_probe_error = f"API 请求失败: {exc}"[:120]

    detail_parts.extend(
        part
        for part in [
            f"API HTTP {api_probe_status}" if api_probe_status else api_probe_error,
            api_geo_hint,
        ]
        if part
    )

    return _service_result(
        "gemini", "Google Gemini", unlocked, status or api_probe_status, detail_parts, region=trace_loc
    )


def _probe_hbo() -> dict[str, Any]:
    endpoints = [
        "https://www.max.com/geo-availability",
        "https://www.max.com/",
        "https://play.max.com/",
    ]
    dns_info = _dns_detail("www.max.com")
    error_detail: str | None = None

    for url in endpoints:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": STREAMING_UA},
                timeout=10,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            error_detail = f"请求失败: {exc}"[:150]
            continue

        status = resp.status_code
        region_match = None
        detail: str | None = None

        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                payload = resp.json()
                region_match = payload.get("countryCode") or payload.get("country")
                detail = payload.get("inAvailableTerritory")
                if isinstance(detail, bool):
                    detail = "已解锁" if detail else "未解锁"
            except (ValueError, TypeError):
                pass

        url_effective = resp.url
        if not detail:
            if status == 200 and "max.com" in url_effective:
                detail = "已解锁"
            elif url_effective.startswith("http://hbogeo.cust.footprint.net") or "geo.html" in url_effective:
                detail = "未解锁"
            else:
                detail = "检测失败"

        detail_parts = [dns_info, f"HTTP {status}", url_effective, detail]
        return _service_result(
            "hbo",
            "HBO",
            detail == "已解锁",
            status,
            detail_parts,
            region=region_match,
        )

    detail_parts = [dns_info, error_detail or "请求失败"]
    return _service_result("hbo", "HBO", False, None, detail_parts)



def _probe_twitch() -> dict[str, Any]:
    dns_info = _dns_detail("www.twitch.tv")
    try:
        resp = requests.get(
            "https://www.twitch.tv",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
        )
    except requests.RequestException as exc:
        return _service_result("twitch", "Twitch", False, None, [dns_info, f"请求失败: {exc}"[:150]])

    status = resp.status_code
    unlocked = status == 200
    detail = "已解锁" if unlocked else "未解锁"
    return _service_result("twitch", "Twitch", unlocked, status, [dns_info, f"HTTP {status}", detail])


def _probe_paramount_plus() -> dict[str, Any]:
    dns_info = _dns_detail("www.paramountplus.com")
    try:
        resp = requests.get(
            "https://www.paramountplus.com",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return _service_result("paramount_plus", "Paramount+", False, None, [dns_info, f"请求失败: {exc}"[:150]])

    status = resp.status_code
    region = None
    if "not available in your country" in resp.text:
        unlocked = False
        detail = "地区限制"
    else:
        # Try to guess region from URL or headers
        region_match = re.search(r"/([a-z]{2})/", resp.url)
        if region_match:
            region = region_match.group(1).upper()
        unlocked = status < 400
        detail = "已解锁" if unlocked else "未解锁"

    detail_parts = [dns_info, f"HTTP {status}", detail, f"Region: {region}" if region else None]
    return _service_result("paramount_plus", "Paramount+", unlocked, status, detail_parts, region=region)


def _probe_bilibili() -> dict[str, Any]:
    dns_info = _dns_detail("www.bilibili.com")
    try:
        # Check streaming availability (Taiwan/HK/Macau only endpoint behavior)
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
        )
    except requests.RequestException as exc:
        return _service_result("bilibili", "Bilibili", False, None, [dns_info, f"请求失败: {exc}"[:150]])

    status = resp.status_code
    unlocked = status == 200
    try:
        data = resp.json()
        if data.get("code") == 0:
            unlocked = True
            detail = "已解锁"
        else:
            unlocked = False
            detail = f"API Error: {data.get('message')}"
    except Exception:
        detail = "响应解析失败"

    return _service_result("bilibili", "Bilibili", unlocked, status, [dns_info, f"HTTP {status}", detail])


def _probe_apple_music() -> dict[str, Any]:
    dns_info = _dns_detail("music.apple.com")
    try:
        resp = requests.get(
            "https://music.apple.com",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return _service_result("apple_music", "Apple Music", False, None, [dns_info, f"请求失败: {exc}"[:150]])

    status = resp.status_code
    unlocked = status == 200
    # Extract region from URL, e.g., https://music.apple.com/us/browse
    match = re.search(r"music\.apple\.com/([a-z]{2})", resp.url)
    region = match.group(1).upper() if match else None
    
    detail = f"Region: {region}" if region else "已解锁"
    return _service_result("apple_music", "Apple Music", unlocked, status, [dns_info, f"HTTP {status}", detail], region=region)


def _probe_telegram() -> dict[str, Any]:
    dns_info = _dns_detail("web.telegram.org")
    try:
        resp = requests.get(
            "https://web.telegram.org/k/",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
        )
    except requests.RequestException as exc:
        return _service_result("telegram", "Telegram", False, None, [dns_info, f"请求失败: {exc}"[:150]])

    status = resp.status_code
    unlocked = status == 200
    detail = "已解锁" if unlocked else "未解锁 (可能受阻)"
    return _service_result("telegram", "Telegram", unlocked, status, [dns_info, f"HTTP {status}", detail])


def _probe_whatsapp() -> dict[str, Any]:
    dns_info = _dns_detail("web.whatsapp.com")
    try:
        resp = requests.get(
            "https://web.whatsapp.com",
            headers={"User-Agent": STREAMING_UA},
            timeout=10,
        )
    except requests.RequestException as exc:
        return _service_result("whatsapp", "WhatsApp", False, None, [dns_info, f"请求失败: {exc}"[:150]])

    status = resp.status_code
    unlocked = status == 200
    detail = "已解锁" if unlocked else "未解锁 (可能受阻)"
    return _service_result("whatsapp", "WhatsApp", unlocked, status, [dns_info, f"HTTP {status}", detail])


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
        _probe_gemini,
        _probe_hbo,
        _probe_twitch,
        _probe_paramount_plus,
        _probe_bilibili,
        _probe_apple_music,
        _probe_telegram,
        _probe_whatsapp,
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


def _get_client_ip() -> str:
    """Extract client IP from request headers or remote_addr."""
    return (
        request.headers.get('X-Real-IP') 
        or request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
        or request.remote_addr 
        or 'unknown'
    )


def _validate_iperf_params(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and sanitize iperf3 parameters to prevent abuse."""
    errors = []
    
    # Validate duration (1-300 seconds)
    duration = data.get("duration", 10)
    try:
        duration = int(duration)
        if duration < 1 or duration > 300:
            errors.append("duration must be between 1 and 300 seconds")
    except (ValueError, TypeError):
        errors.append("duration must be an integer")
    
    # Validate parallel connections (1-128)
    parallel = data.get("parallel", 1)
    try:
        parallel = int(parallel)
        if parallel < 1 or parallel > 128:
            errors.append("parallel must be between 1 and 128")
    except (ValueError, TypeError):
        errors.append("parallel must be an integer")
    
    # Validate protocol
    protocol = data.get("protocol", "tcp")
    if protocol.lower() not in ["tcp", "udp"]:
        errors.append("protocol must be 'tcp' or 'udp'")
    
    # Validate omit (0-60 seconds)
    omit = data.get("omit")
    if omit is not None:
        try:
            omit = int(omit)
            if omit < 0 or omit > 60:
                errors.append("omit must be between 0 and 60 seconds")
        except (ValueError, TypeError):
            errors.append("omit must be an integer")
    
    if errors:
        raise ValueError("; ".join(errors))
    
    return {
        "duration": duration,
        "parallel": parallel,
        "protocol": protocol.lower(),
        "omit": omit
    }


def require_whitelisted_ip(f):
    """Decorator to enforce IP whitelist validation on endpoints."""
    from functools import wraps
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        client_ip = _get_client_ip()
        
        # DEBUG: Log all request details for troubleshooting
        app.logger.info(f"[DEBUG] Whitelist Check - Client IP: {client_ip}")
        app.logger.info(f"[DEBUG] Headers: X-Real-IP={request.headers.get('X-Real-IP')}, "
                       f"X-Forwarded-For={request.headers.get('X-Forwarded-For')}, "
                       f"Remote-Addr={request.remote_addr}")
        app.logger.info(f"[DEBUG] Current Whitelist: {whitelist.get_all()}")
        
        if not whitelist.is_allowed(client_ip):
            app.logger.warning(
                f"[SECURITY] Rejected request from non-whitelisted IP: {client_ip} "
                f"to endpoint: {request.endpoint}"
            )
            return jsonify({
                "status": "error",
                "error": "IP not whitelisted",
                "message": "Your IP address is not authorized to access this service. "
                          "Contact the administrator to add your IP to the whitelist.",
                "client_ip": client_ip,
                "allowed_ips": whitelist.get_all(),
                "debug": {
                    "x_real_ip": request.headers.get('X-Real-IP'),
                    "x_forwarded_for": request.headers.get('X-Forwarded-For'),
                    "remote_addr": request.remote_addr
                }
            }), 403
        
        app.logger.info(f"[ACCESS] Request from whitelisted IP: {client_ip} to {request.endpoint}")
        return f(*args, **kwargs)
    
    return decorated_function


def _create_error_response(error_type: str, message: str, status_code: int = 400, **extra) -> tuple:
    """Create standardized error response."""
    response = {
        "status": "error",
        "error": error_type,
        "message": message,
        "timestamp": int(time.time())
    }
    response.update(extra)
    return jsonify(response), status_code


def _start_server_process(port: int) -> subprocess.Popen:
    cmd = f"iperf3 -s -p {port}"
    return subprocess.Popen(shlex.split(cmd))


def _is_process_running(proc: subprocess.Popen | None) -> bool:
    return proc is not None and proc.poll() is None


@app.route("/health", methods=["GET"])
def health() -> Any:
    streaming_payload = _load_cached_probe(max_age=STREAMING_AUTO_INTERVAL)
    streaming_results = None
    streaming_timestamp = None
    if streaming_payload:
        streaming_results = streaming_payload.get("results")
        streaming_timestamp = streaming_payload.get("timestamp")

    return jsonify({
        "status": "ok",
        "version": AGENT_VERSION,
        "server_running": _is_process_running(server_process),
        "port": DEFAULT_IPERF_PORT,
        "timestamp": int(time.time()),
        "backbone_latency": _get_backbone_latency(),
        "streaming": streaming_results,
        "streaming_checked_at": streaming_timestamp,
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
@require_whitelisted_ip
def run_test() -> Any:
    """
    Execute iperf3 test with IP whitelist validation and enhanced error handling.
    
    This endpoint requires the client IP to be in the whitelist.
    All parameters are validated before execution.
    """
    client_ip = _get_client_ip()
    
    try:
        data = request.get_json(force=True)
    except Exception as e:
        app.logger.error(f"[ERROR] Failed to parse JSON from {client_ip}: {e}")
        return _create_error_response("invalid_json", "Request body must be valid JSON")
    
    # Validate required fields
    target = data.get("target")
    if not target:
        return _create_error_response("missing_parameter", "Missing required parameter: target")
    
    # Validate target is not a local/private IP to prevent SSRF
    try:
        import ipaddress
        target_ip = ipaddress.ip_address(target)
        if target_ip.is_loopback and client_ip not in ["127.0.0.1", "::1"]:
            return _create_error_response(
                "invalid_target", 
                "Cannot target loopback addresses from remote clients"
            )
    except ValueError:
        # Not an IP address, might be hostname - allow it
        pass
    
    # Validate and sanitize parameters
    try:
        validated_params = _validate_iperf_params(data)
        duration = validated_params["duration"]
        parallel = validated_params["parallel"]
        protocol = validated_params["protocol"]
        omit = validated_params["omit"]
    except ValueError as e:
        app.logger.warning(f"[VALIDATION] Parameter validation failed from {client_ip}: {e}")
        return _create_error_response("invalid_parameters", str(e))
    
    # Read optional parameters
    try:
        port = _read_port_from_request(data)
        reverse_mode = str(data.get("reverse", "false")).lower() in ["1", "true", "yes"]
        bandwidth = data.get("bandwidth")
        datagram_size = data.get("datagram_size")
    except ValueError as e:
        return _create_error_response("invalid_parameter", str(e))
    
    # Build iperf3 command
    proto_flag = "-u" if protocol == "udp" else ""
    reverse_flag = "-R" if reverse_mode else ""
    extra_flags: list[str] = []
    
    if bandwidth:
        extra_flags.extend(["-b", str(bandwidth)])
    if datagram_size and protocol == "udp":
        extra_flags.extend(["-l", str(datagram_size)])
    if omit:
        extra_flags.extend(["-O", str(omit)])
    if reverse_mode:
        extra_flags.append("--get-server-output")

    cmd_parts = [
        "iperf3",
        "-c",
        str(target),
        "-p",
        str(port),
        "-t",
        str(duration),
        "-P",
        str(parallel),
    ]

    if proto_flag:
        cmd_parts.append(proto_flag)
    if reverse_flag:
        cmd_parts.append(reverse_flag)
    cmd_parts.extend(extra_flags)
    cmd_parts.append("-J")

    cmd = " ".join(cmd_parts)
    
    app.logger.info(
        f"[TEST] Starting iperf3 test from {client_ip} to {target}:{port} "
        f"({protocol}, {duration}s, P={parallel})"
    )

    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=duration + 15,
        )
    except subprocess.TimeoutExpired:
        app.logger.error(f"[TIMEOUT] Test from {client_ip} to {target} timed out")
        return _create_error_response("timeout", "Test execution timed out", 504)

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip() or f"iperf3 failed with exit code {result.returncode}"
        app.logger.error(f"[FAILED] Test from {client_ip} failed: {error_msg}")
        return _create_error_response("test_failed", error_msg, 500)

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        app.logger.error(f"[PARSE_ERROR] Failed to parse iperf3 output from {client_ip}: {e}")
        return _create_error_response("parse_failed", "Failed to parse iperf3 output", 500)

    app.logger.info(f"[SUCCESS] Test from {client_ip} to {target} completed successfully")
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


@app.route("/whitelist", methods=["GET"])
def get_whitelist() -> Any:
    """Get current whitelist."""
    return jsonify({
        "status": "ok",
        "allowed_ips": whitelist.get_all(),
        "updated_at": None
    })


@app.route("/update_whitelist", methods=["POST"])
def update_whitelist() -> Any:
    """
    Update IP whitelist from Master.
    Only accepts requests from Master IP (configured via MASTER_IP env var).
    """
    # Get client IP using shared function for consistency
    client_ip = _get_client_ip()
    master_ip = os.getenv("MASTER_IP", "")
    
    # DEBUG: Log request details
    app.logger.info(f"[DEBUG] Whitelist Update Request - Client IP: {client_ip}, Configured Master: {master_ip}")
    app.logger.info(f"[DEBUG] Headers: X-Real-IP={request.headers.get('X-Real-IP')}, "
                   f"X-Forwarded-For={request.headers.get('X-Forwarded-For')}, "
                   f"Remote-Addr={request.remote_addr}")
    
    # Check if MASTER_IP is set and verify caller
    if master_ip and master_ip != "0.0.0.0" and client_ip != master_ip:
        app.logger.warning(f"[SECURITY] Rejected whitelist update from {client_ip} (expected {master_ip})")
        return jsonify({
            "status": "error",
            "error": f"Only Master can update whitelist. Request from {client_ip}, expected {master_ip}"
        }), 403
    
    try:
        data = request.get_json(force=True)
        allowed_ips = data.get("allowed_ips", [])
        
        if not isinstance(allowed_ips, list):
            return jsonify({"status": "error", "error": "allowed_ips must be a list"}), 400
        
        # Update whitelist
        whitelist.update(allowed_ips)
        
        app.logger.info(f"Whitelist updated with {len(allowed_ips)} IPs from Master")
        
        return jsonify({
            "status": "ok",
            "message": f"Whitelist updated with {len(allowed_ips)} IPs",
            "allowed_ips": whitelist.get_all()
        })
    except Exception as e:
        app.logger.error(f"Failed to update whitelist: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/merge_whitelist", methods=["POST"])
def merge_whitelist() -> Any:
    """
    Merge IPs into whitelist (APPEND mode - keeps existing IPs).
    
    This is designed for multi-Master scenarios where multiple Masters
    share the same Agent. Each Master can add its IPs without removing
    IPs added by other Masters.
    """
    client_ip = _get_client_ip()
    
    app.logger.info(f"[DEBUG] Whitelist Merge Request - Client IP: {client_ip}")
    
    try:
        data = request.get_json(force=True)
        ips_to_add = data.get("allowed_ips", [])
        
        if not isinstance(ips_to_add, list):
            return jsonify({"status": "error", "error": "allowed_ips must be a list"}), 400
        
        # Merge IPs into whitelist
        result = whitelist.merge(ips_to_add)
        
        app.logger.info(f"Whitelist merged: {result['added']} added, {result['skipped']} skipped from {client_ip}")
        
        return jsonify({
            "status": "ok",
            "message": f"Merged {result['added']} new IPs, {result['skipped']} already existed",
            "result": result,
            "allowed_ips": whitelist.get_all()
        })
    except Exception as e:
        app.logger.error(f"Failed to merge whitelist: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/remove_whitelist_ips", methods=["POST"])
def remove_whitelist_ips() -> Any:
    """
    Remove specific IPs from whitelist.
    """
    client_ip = _get_client_ip()
    
    app.logger.info(f"[DEBUG] Whitelist Remove Request - Client IP: {client_ip}")
    
    try:
        data = request.get_json(force=True)
        ips_to_remove = data.get("ips", [])
        
        if not isinstance(ips_to_remove, list):
            return jsonify({"status": "error", "error": "ips must be a list"}), 400
        
        # Remove IPs from whitelist
        result = whitelist.remove_ips(ips_to_remove)
        
        app.logger.info(f"Whitelist IPs removed: {result['removed']} removed from {client_ip}")
        
        return jsonify({
            "status": "ok",
            "message": f"Removed {result['removed']} IPs, {result['not_found']} not found",
            "result": result,
            "allowed_ips": whitelist.get_all()
        })
    except Exception as e:
        app.logger.error(f"Failed to remove whitelist IPs: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500



@app.route("/whitelist/stats", methods=["GET"])
def get_whitelist_stats() -> Any:
    """Get whitelist statistics"""
    stats = whitelist.get_statistics()
    return jsonify({
        "status": "ok",
        "statistics": stats
    })


# ============== Reverse Mode Polling Logic ==============

def _execute_test_task(task: dict) -> dict:
    """Execute a test task received from master and return result."""
    target = task.get("target_ip")
    port = task.get("target_port", 5201)
    duration = task.get("duration", 10)
    protocol = task.get("protocol", "tcp")
    parallel = task.get("parallel", 1)
    reverse_mode = task.get("reverse", False)
    bandwidth = task.get("bandwidth")
    
    proto_flag = "-u" if protocol == "udp" else ""
    reverse_flag = "-R" if reverse_mode else ""
    extra_flags: list[str] = []
    
    if bandwidth:
        extra_flags.extend(["-b", str(bandwidth)])
    if reverse_mode:
        extra_flags.append("--get-server-output")
    
    cmd_parts = [
        "iperf3", "-c", str(target), "-p", str(port),
        "-t", str(duration), "-P", str(parallel),
    ]
    
    if proto_flag:
        cmd_parts.append(proto_flag)
    if reverse_flag:
        cmd_parts.append(reverse_flag)
    cmd_parts.extend(extra_flags)
    cmd_parts.append("-J")
    
    try:
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=duration + 30,
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or f"iperf3 exit code {result.returncode}"
            return {"status": "error", "error": error_msg}
        
        output = json.loads(result.stdout)
        return {"status": "ok", "iperf_result": output}
        
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Test execution timed out"}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Failed to parse iperf3 output: {e}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _reverse_mode_register():
    """Register this agent with the master."""
    if not MASTER_URL or not NODE_NAME:
        return False
    
    register_url = f"{MASTER_URL.rstrip('/')}/api/agent/register"
    payload = {
        "node_name": NODE_NAME,
        "iperf_port": IPERF_PORT,
        "agent_version": AGENT_VERSION,
    }
    
    try:
        resp = requests.post(register_url, json=payload, timeout=10)
        if resp.ok:
            app.logger.info(f"[REVERSE] Registered with master: {NODE_NAME}")
            return True
        else:
            app.logger.warning(f"[REVERSE] Registration failed: {resp.status_code}")
            return False
    except Exception as e:
        app.logger.warning(f"[REVERSE] Registration error: {e}")
        return False


def _reverse_mode_poll():
    """Poll master for pending tasks."""
    if not MASTER_URL or not NODE_NAME:
        return None
    
    tasks_url = f"{MASTER_URL.rstrip('/')}/api/agent/tasks"
    params = {"node_name": NODE_NAME}
    
    try:
        resp = requests.get(tasks_url, params=params, timeout=10)
        if resp.ok:
            data = resp.json()
            tasks = data.get("tasks", [])
            return tasks
        return []
    except Exception as e:
        app.logger.debug(f"[REVERSE] Poll error: {e}")
        return []


def _reverse_mode_report_result(task_id: int, result: dict):
    """Report task result back to master."""
    if not MASTER_URL:
        return
    
    result_url = f"{MASTER_URL.rstrip('/')}/api/agent/tasks/{task_id}/result"
    payload = {
        "node_name": NODE_NAME,
        "result": result,
    }
    
    try:
        resp = requests.post(result_url, json=payload, timeout=10)
        if resp.ok:
            app.logger.info(f"[REVERSE] Result reported for task {task_id}")
        else:
            app.logger.warning(f"[REVERSE] Failed to report result: {resp.status_code}")
    except Exception as e:
        app.logger.warning(f"[REVERSE] Report error: {e}")


def _reverse_mode_loop():
    """Background loop for reverse mode polling."""
    global _reverse_running
    _reverse_running = True
    
    app.logger.info(f"[REVERSE] Starting reverse mode: master={MASTER_URL}, node={NODE_NAME}")
    
    # Initial registration
    registered = False
    while _reverse_running and not registered:
        registered = _reverse_mode_register()
        if not registered:
            time.sleep(POLL_INTERVAL)
    
    # Polling loop
    last_register = time.time()
    while _reverse_running:
        try:
            # Re-register periodically (every 5 minutes)
            if time.time() - last_register > 300:
                _reverse_mode_register()
                last_register = time.time()
            
            # Poll for tasks
            tasks = _reverse_mode_poll()
            if tasks:
                for task in tasks:
                    task_id = task.get("id")
                    app.logger.info(f"[REVERSE] Executing task {task_id}: {task.get('target_ip')}")
                    result = _execute_test_task(task)
                    _reverse_mode_report_result(task_id, result)
            
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            app.logger.error(f"[REVERSE] Loop error: {e}")
            time.sleep(POLL_INTERVAL)


def start_reverse_mode():
    """Start the reverse mode polling thread."""
    global _reverse_thread
    if AGENT_MODE != "reverse":
        return
    
    if not MASTER_URL or not NODE_NAME:
        app.logger.error("[REVERSE] Missing MASTER_URL or NODE_NAME for reverse mode")
        return
    
    _reverse_thread = threading.Thread(target=_reverse_mode_loop, daemon=True)
    _reverse_thread.start()


if __name__ == "__main__":
    # Start reverse mode polling if configured
    if AGENT_MODE == "reverse":
        start_reverse_mode()
    
    app.run(host="0.0.0.0", port=AGENT_API_PORT)

