"""
Alert Notification Service

Handles sending alerts via Telegram, Webhook, and rate limiting.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Rate limiting: track last alert time per (alert_type, node_id)
_alert_cooldowns: dict[str, datetime] = {}
ALERT_COOLDOWN_SECONDS = 300  # 5 minutes between same alerts


async def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    """Send message via Telegram Bot API."""
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured (missing bot_token or chat_id)")
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                logger.info(f"Telegram alert sent to chat {chat_id}")
                return True
            else:
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False


async def send_webhook(webhook_url: str, payload: dict) -> bool:
    """Send alert via HTTP webhook."""
    if not webhook_url:
        logger.warning("Webhook not configured")
        return False
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=payload)
            if response.status_code in (200, 201, 204):
                logger.info(f"Webhook alert sent to {webhook_url}")
                return True
            else:
                logger.error(f"Webhook error: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")
        return False


def _get_cooldown_key(alert_type: str, node_id: Optional[int]) -> str:
    """Generate unique key for rate limiting."""
    return f"{alert_type}:{node_id or 'global'}"


def is_rate_limited(alert_type: str, node_id: Optional[int] = None) -> bool:
    """Check if alert is rate limited (within cooldown period)."""
    key = _get_cooldown_key(alert_type, node_id)
    last_alert = _alert_cooldowns.get(key)
    
    if last_alert:
        elapsed = (datetime.now(timezone.utc) - last_alert).total_seconds()
        if elapsed < ALERT_COOLDOWN_SECONDS:
            logger.debug(f"Alert {key} rate limited, {ALERT_COOLDOWN_SECONDS - elapsed:.0f}s remaining")
            return True
    
    return False


def record_alert_sent(alert_type: str, node_id: Optional[int] = None):
    """Record that an alert was sent for rate limiting."""
    key = _get_cooldown_key(alert_type, node_id)
    _alert_cooldowns[key] = datetime.now(timezone.utc)


def clear_cooldown(alert_type: str, node_id: Optional[int] = None):
    """Clear rate limit cooldown for an alert (e.g., when resolved)."""
    key = _get_cooldown_key(alert_type, node_id)
    _alert_cooldowns.pop(key, None)


def format_alert_message(alert_type: str, severity: str, node_name: str, message: str, details: dict = None) -> str:
    """Format alert message for Telegram (HTML)."""
    severity_emoji = {
        "info": "ℹ️",
        "warning": "⚠️",
        "critical": "🚨"
    }
    
    alert_type_labels = {
        "ping_high": "高延迟告警",
        "node_offline": "节点离线",
        "bandwidth_low": "带宽异常",
        "route_change": "路由变化",
        "test_alert": "测试告警"
    }
    
    emoji = severity_emoji.get(severity, "📢")
    label = alert_type_labels.get(alert_type, alert_type)
    
    text = f"{emoji} <b>{label}</b>\n"
    text += f"━━━━━━━━━━━━━━\n"
    if node_name:
        text += f"📍 节点: <code>{node_name}</code>\n"
    text += f"📝 {message}\n"
    
    if details:
        if details.get("current_value"):
            text += f"📊 当前值: <code>{details['current_value']}</code>\n"
        if details.get("threshold"):
            text += f"🎯 阈值: <code>{details['threshold']}</code>\n"
    
    text += f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
    
    return text


def format_webhook_payload(alert_type: str, severity: str, node_id: int, node_name: str, message: str, details: dict = None) -> dict:
    """Format alert payload for webhook."""
    return {
        "alert_type": alert_type,
        "severity": severity,
        "node_id": node_id,
        "node_name": node_name,
        "message": message,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
