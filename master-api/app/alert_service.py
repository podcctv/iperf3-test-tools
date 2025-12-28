"""
Alert Notification Service

Handles sending alerts via Telegram, Webhook, and rate limiting.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

# GMT+8 Beijing timezone
TZ_BEIJING = timezone(timedelta(hours=8))

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
    Modern tech aesthetic with clean visual design.
    
    Args:
        node_name: Name of the offline node
        node_ip: IP address of the node (will be partially masked if public)
        offline_since: When the node went offline
    
    Returns:
        HTML formatted message for Telegram
    """
    now = datetime.now(TZ_BEIJING)
    duration_seconds = (now - offline_since).total_seconds()
    duration_str = _format_duration(duration_seconds)
    
    # Mask IP for privacy (show first two octets only)
    ip_parts = node_ip.split(".")
    if len(ip_parts) == 4:
        masked_ip = f"{ip_parts[0]}.{ip_parts[1]}.*.*"
    else:
        masked_ip = node_ip
    
    text = f"<b>⬤ 节点离线</b>\n"
    text += f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    text += f"▸ 节点: <code>{node_name}</code>\n"
    text += f"▸ 地址: <code>{masked_ip}</code>\n"
    text += f"▸ 时长: <b>{duration_str}</b>\n"
    text += f"▸ 开始: {offline_since.astimezone(TZ_BEIJING).strftime('%H:%M:%S')}\n"
    text += f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    text += f"<i>◷ {now.strftime('%H:%M:%S')}</i>"
    
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


# ============================================================================
# Daily Offline Statistics Card
# ============================================================================

def format_daily_stats_card(date_str: str, node_stats: list, current_offline: dict) -> str:
    """Format daily offline statistics card for Telegram (HTML).
    Modern tech aesthetic with clean visual design.
    
    Args:
        date_str: Date string like "2025-12-28"
        node_stats: List of dicts with node offline stats
        current_offline: Dict of currently offline nodes: {node_id: seconds_offline}
    
    Returns:
        HTML formatted message for Telegram
    """
    now = datetime.now(TZ_BEIJING)
    
    # Header with tech aesthetic
    text = f"<b>◈ 节点监控面板</b>\n"
    text += f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    text += f"◷ {date_str}  ·  实时监控中\n\n"
    
    # Filter nodes that have offline events today
    nodes_with_events = [s for s in node_stats if s["offline_count"] > 0]
    
    if not nodes_with_events:
        text += "◉ <i>全部节点运行正常</i>\n\n"
    else:
        text += "<b>▸ 异常节点</b>\n\n"
        for stat in nodes_with_events:
            node_name = stat["node_name"]
            node_id = stat.get("node_id")
            offline_count = stat["offline_count"]
            total_duration = stat["total_duration"]  # seconds
            
            # Check if currently offline
            is_offline = node_id in current_offline if node_id else False
            current_offline_duration = current_offline.get(node_id, 0) if node_id else 0
            
            if is_offline:
                text += f"▣ <b>{node_name}</b>  ⬤ 离线\n"
            else:
                text += f"▢ <b>{node_name}</b>  ○ 已恢复\n"
            
            text += f"   ├ 中断: {offline_count}次\n"
            text += f"   └ 累计: {_format_duration(total_duration)}"
            
            if is_offline:
                text += f" (+{_format_duration(current_offline_duration)})"
            text += "\n\n"
    
    # Summary section
    text += f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    
    total_nodes = len(node_stats)
    nodes_with_offline = len(nodes_with_events)
    nodes_normal = total_nodes - nodes_with_offline
    current_offline_count = len(current_offline)
    
    text += f"<b>▸ 状态汇总</b>\n"
    text += f"   正常运行: {nodes_normal}/{total_nodes}\n"
    if nodes_with_offline > 0:
        text += f"   今日异常: {nodes_with_offline}\n"
    if current_offline_count > 0:
        text += f"   当前离线: {current_offline_count}\n"
    
    text += f"\n<i>◷ {now.strftime('%H:%M:%S')}</i>"
    
    return text


def format_daily_archive_card(date_str: str, node_stats: list) -> str:
    """Format end-of-day archive card for Telegram (HTML).
    This card is sent at the end of day and persists in chat history.
    
    Args:
        date_str: Date string like "2025-12-28"
        node_stats: List of dicts with node offline stats
    
    Returns:
        HTML formatted message for Telegram
    """
    # Header
    text = f"<b>◈ 每日运维报告</b>\n"
    text += f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
    text += f"◷ {date_str}\n\n"
    
    # Filter nodes that have offline events today
    nodes_with_events = [s for s in node_stats if s["offline_count"] > 0]
    total_nodes = len(node_stats)
    nodes_with_offline = len(nodes_with_events)
    nodes_normal = total_nodes - nodes_with_offline
    
    # Calculate total offline time across all nodes
    total_offline_seconds = sum(s["total_duration"] for s in nodes_with_events)
    total_offline_count = sum(s["offline_count"] for s in nodes_with_events)
    
    if not nodes_with_events:
        text += "◉ <b>全天无故障</b>\n\n"
        text += f"   所有 {total_nodes} 个节点运行正常\n"
        text += f"   可用率: 100%\n\n"
    else:
        text += "<b>▸ 故障统计</b>\n\n"
        
        # Sort by total duration (worst first)
        sorted_stats = sorted(nodes_with_events, key=lambda x: x["total_duration"], reverse=True)
        
        for stat in sorted_stats:
            node_name = stat["node_name"]
            offline_count = stat["offline_count"]
            total_duration = stat["total_duration"]
            
            # Calculate uptime percentage (assuming 24h day)
            uptime_pct = max(0, 100 - (total_duration / 864) * 100)  # 86400s = 24h
            
            text += f"▢ <b>{node_name}</b>\n"
            text += f"   ├ 中断: {offline_count}次\n"
            text += f"   ├ 时长: {_format_duration(total_duration)}\n"
            text += f"   └ 可用率: {uptime_pct:.1f}%\n\n"
        
        text += f"▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        text += f"<b>▸ 日总结</b>\n"
        text += f"   正常节点: {nodes_normal}/{total_nodes}\n"
        text += f"   异常节点: {nodes_with_offline}\n"
        text += f"   总中断: {total_offline_count}次\n"
        text += f"   总时长: {_format_duration(total_offline_seconds)}\n"
    
    text += f"\n<i>— 报告生成于 {datetime.now(TZ_BEIJING).strftime('%Y-%m-%d %H:%M')} —</i>"
    
    return text


async def send_daily_archive_card(bot_token: str, chat_id: str, date_str: str, node_stats: list) -> Optional[int]:
    """Send end-of-day archive card via Telegram.
    
    Returns:
        message_id if successful, None otherwise
    """
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured for daily archive")
        return None
    
    message = format_daily_archive_card(date_str, node_stats)
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
                logger.info(f"Daily archive card sent for {date_str}, message_id={message_id}")
                return message_id
            else:
                logger.error(f"Telegram API error sending archive: {response.status_code} - {response.text}")
                return None
    except Exception as e:
        logger.error(f"Failed to send daily archive card: {e}")
        return None


async def send_daily_stats_card(bot_token: str, chat_id: str, date_str: str, 
                                 node_stats: list, current_offline: dict) -> Optional[int]:
    """Send daily statistics card via Telegram and return message_id.
    
    Returns:
        message_id if successful, None otherwise
    """
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured for daily stats card")
        return None
    
    message = format_daily_stats_card(date_str, node_stats, current_offline)
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
                logger.info(f"Daily stats card sent for {date_str}, message_id={message_id}")
                return message_id
            else:
                logger.error(f"Telegram API error sending daily stats: {response.status_code} - {response.text}")
                return None
    except Exception as e:
        logger.error(f"Failed to send daily stats card: {e}")
        return None


async def edit_daily_stats_card(bot_token: str, chat_id: str, message_id: int,
                                 date_str: str, node_stats: list, current_offline: dict) -> bool:
    """Update existing daily statistics card.
    
    Returns:
        True if successful, False otherwise
    """
    if not bot_token or not chat_id or not message_id:
        logger.warning("Cannot edit daily stats: missing parameters")
        return False
    
    message = format_daily_stats_card(date_str, node_stats, current_offline)
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
                logger.debug(f"Daily stats card updated for {date_str}")
                return True
            elif response.status_code == 400:
                error_data = response.json()
                if "message is not modified" in error_data.get("description", "").lower():
                    logger.debug(f"Daily stats card not modified (same content)")
                    return True
                logger.error(f"Telegram API error editing daily stats: {response.status_code} - {response.text}")
                return False
            else:
                logger.error(f"Telegram API error editing daily stats: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Failed to edit daily stats card: {e}")
        return False


