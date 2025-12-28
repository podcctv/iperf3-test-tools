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


# ============================================================================
# Offline Node Card System - Send/Update/Delete Telegram Cards
# ============================================================================

def _format_duration(seconds: float) -> str:
    """Format duration in human-readable Chinese format."""
    if seconds < 60:
        return f"{int(seconds)}秒"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}分{secs}秒"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}小时{minutes}分"


def format_offline_card(node_name: str, node_ip: str, offline_since: datetime) -> str:
    """Format offline node card message for Telegram (HTML).
    
    Args:
        node_name: Name of the offline node
        node_ip: IP address of the node (will be partially masked if public)
        offline_since: When the node went offline
    
    Returns:
        HTML formatted message for Telegram
    """
    now = datetime.now(timezone.utc)
    duration_seconds = (now - offline_since).total_seconds()
    duration_str = _format_duration(duration_seconds)
    
    # Mask IP for privacy (show first two octets only)
    ip_parts = node_ip.split(".")
    if len(ip_parts) == 4:
        masked_ip = f"{ip_parts[0]}.{ip_parts[1]}.xxx.xxx"
    else:
        masked_ip = node_ip
    
    text = f"🔴 <b>节点离线告警</b>\n"
    text += f"━━━━━━━━━━━━━━\n"
    text += f"📍 节点: <code>{node_name}</code>\n"
    text += f"📡 IP: <code>{masked_ip}</code>\n"
    text += f"⏱️ 离线时长: <b>{duration_str}</b>\n"
    text += f"🕐 开始时间: {offline_since.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
    text += f"\n<i>节点无法连接，请检查网络状况</i>\n"
    text += f"━━━━━━━━━━━━━━\n"
    text += f"<i>最后更新: {now.strftime('%H:%M:%S')} UTC</i>"
    
    return text


async def send_offline_card(bot_token: str, chat_id: str, node_name: str, node_ip: str, offline_since: datetime) -> Optional[int]:
    """Send offline node card via Telegram and return message_id.
    
    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        node_name: Name of the offline node
        node_ip: IP address of the node
        offline_since: When the node went offline
    
    Returns:
        message_id if successful, None otherwise
    """
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured (missing bot_token or chat_id)")
        return None
    
    message = format_offline_card(node_name, node_ip, offline_since)
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
                data = response.json()
                message_id = data.get("result", {}).get("message_id")
                logger.info(f"Offline card sent for {node_name}, message_id={message_id}")
                return message_id
            else:
                logger.error(f"Telegram API error sending offline card: {response.status_code} - {response.text}")
                return None
    except Exception as e:
        logger.error(f"Failed to send offline card: {e}")
        return None


async def edit_offline_card(bot_token: str, chat_id: str, message_id: int, node_name: str, node_ip: str, offline_since: datetime) -> bool:
    """Update existing offline node card with new duration.
    
    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        message_id: ID of message to edit
        node_name: Name of the offline node
        node_ip: IP address of the node
        offline_since: When the node went offline
    
    Returns:
        True if successful, False otherwise
    """
    if not bot_token or not chat_id or not message_id:
        logger.warning("Cannot edit message: missing parameters")
        return False
    
    message = format_offline_card(node_name, node_ip, offline_since)
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                logger.debug(f"Offline card updated for {node_name}, message_id={message_id}")
                return True
            elif response.status_code == 400:
                # Check if message was not modified (same content)
                error_data = response.json()
                if "message is not modified" in error_data.get("description", "").lower():
                    logger.debug(f"Offline card not modified (same content) for {node_name}")
                    return True
                logger.error(f"Telegram API error editing offline card: {response.status_code} - {response.text}")
                return False
            else:
                logger.error(f"Telegram API error editing offline card: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Failed to edit offline card: {e}")
        return False


async def delete_telegram_message(bot_token: str, chat_id: str, message_id: int) -> bool:
    """Delete a Telegram message.
    
    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        message_id: ID of message to delete
    
    Returns:
        True if successful, False otherwise
    """
    if not bot_token or not chat_id or not message_id:
        logger.warning("Cannot delete message: missing parameters")
        return False
    
    url = f"https://api.telegram.org/bot{bot_token}/deleteMessage"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                logger.info(f"Telegram message deleted: message_id={message_id}")
                return True
            elif response.status_code == 400:
                # Message may already be deleted or too old
                error_data = response.json()
                error_desc = error_data.get("description", "").lower()
                if "message to delete not found" in error_desc or "message can't be deleted" in error_desc:
                    logger.warning(f"Message {message_id} already deleted or cannot be deleted")
                    return True  # Consider this success since message is gone
                logger.error(f"Telegram API error deleting message: {response.status_code} - {response.text}")
                return False
            else:
                logger.error(f"Telegram API error deleting message: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Failed to delete Telegram message: {e}")
        return False

