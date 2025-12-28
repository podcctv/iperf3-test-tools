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
    """Format duration in short English format for terminal alignment."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


class TerminalBox:
    """Helper class to build dynamically aligned terminal-style boxes."""
    
    def __init__(self, min_width: int = 22, max_width: int = 24):
        self.lines = []  # List of (type, content) where type is 'header', 'separator', 'content', 'empty'
        self.min_width = min_width
        self.max_width = max_width  # Limit for mobile display
    
    def header(self, title: str):
        """Add header line: ┌─ TITLE ─────┐"""
        self.lines.append(('header', title))
        return self
    
    def separator(self, title: str = None):
        """Add separator line: ├─ TITLE ─────┤ or ├─────────────┤"""
        self.lines.append(('separator', title))
        return self
    
    def content(self, text: str):
        """Add content line: │  text       │"""
        self.lines.append(('content', text))
        return self
    
    def empty(self):
        """Add empty line: │             │"""
        self.lines.append(('empty', None))
        return self
    
    def footer(self):
        """Add footer line: └─────────────┘"""
        self.lines.append(('footer', None))
        return self
    
    def _get_display_width(self, text: str) -> int:
        """Calculate display width (Chinese chars count as 2)."""
        width = 0
        for char in text:
            if '\u4e00' <= char <= '\u9fff' or '\u3000' <= char <= '\u303f':
                width += 2  # Chinese characters
            else:
                width += 1
        return width
    
    def _pad_to_width(self, text: str, target_width: int) -> str:
        """Pad text to target width, accounting for Chinese characters."""
        current_width = self._get_display_width(text)
        if current_width >= target_width:
            return text
        return text + ' ' * (target_width - current_width)
    
    def build(self) -> str:
        """Build the final box string with dynamic width."""
        # Calculate max content width, but limit to max_width
        max_content_width = 0
        for line_type, content in self.lines:
            if line_type == 'content' and content:
                max_content_width = max(max_content_width, self._get_display_width(content))
            elif line_type in ('header', 'separator') and content:
                max_content_width = max(max_content_width, self._get_display_width(content) + 4)
        
        # Add padding (2 spaces each side) + apply limits
        inner_width = max(max_content_width + 4, self.min_width)
        inner_width = min(inner_width, self.max_width)  # Apply max limit for mobile
        
        result = []
        for line_type, content in self.lines:
            if line_type == 'header':
                # ┌─ TITLE ─────────────┐
                title_part = f"─ {content} "
                title_display_width = self._get_display_width(title_part)
                remaining = max(0, inner_width - title_display_width)
                line = f"┌{title_part}{'─' * remaining}┐"
            elif line_type == 'separator':
                if content:
                    # ├─ TITLE ─────────────┤
                    title_part = f"─ {content} "
                    title_display_width = self._get_display_width(title_part)
                    remaining = max(0, inner_width - title_display_width)
                    line = f"├{title_part}{'─' * remaining}┤"
                else:
                    # ├──────────────────────┤
                    line = f"├{'─' * inner_width}┤"
            elif line_type == 'content':
                # │  content             │
                padded = self._pad_to_width(f"  {content}", inner_width)
                line = f"│{padded}│"
            elif line_type == 'empty':
                # │                      │
                line = f"│{' ' * inner_width}│"
            elif line_type == 'footer':
                # └──────────────────────┘
                line = f"└{'─' * inner_width}┘"
            else:
                continue
            result.append(f"<code>{line}</code>")
        
        return "\n".join(result)


def format_offline_card(node_name: str, node_ip: str, offline_since: datetime) -> str:
    """Format offline node card message for Telegram (HTML).
    Terminal-style design with dynamic width alignment.
    """
    now = datetime.now(TZ_BEIJING)
    duration_seconds = (now - offline_since).total_seconds()
    duration_str = _format_duration(duration_seconds)
    since_str = offline_since.astimezone(TZ_BEIJING).strftime('%H:%M:%S')
    
    # Mask IP for privacy
    ip_parts = node_ip.split(".")
    if len(ip_parts) == 4:
        masked_ip = f"{ip_parts[0]}.{ip_parts[1]}.*.*"
    else:
        masked_ip = node_ip
    
    box = TerminalBox()
    box.header("ALERT")
    box.empty()
    box.content("■ NODE OFFLINE")
    box.empty()
    box.separator()
    box.content(f"NAME   {node_name}")
    box.content(f"ADDR   {masked_ip}")
    box.content(f"DOWN   {duration_str}")
    box.content(f"SINCE  {since_str}")
    box.separator()
    box.content(f"UPD {now.strftime('%H:%M:%S')}")
    box.footer()
    
    return box.build()


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
    Terminal-style design with dynamic width alignment.
    """
    now = datetime.now(TZ_BEIJING)
    total_nodes = len(node_stats)
    nodes_with_events = [s for s in node_stats if s["offline_count"] > 0]
    current_offline_count = len(current_offline)
    online_count = total_nodes - current_offline_count
    
    box = TerminalBox()
    box.header("NODE STATUS")
    box.empty()
    box.content(f"{date_str}   ● LIVE")
    box.empty()
    
    if not nodes_with_events:
        box.content("✓ ALL SYSTEMS ONLINE")
        box.empty()
    else:
        box.separator("INCIDENTS")
        box.empty()
        
        for stat in nodes_with_events:
            node_name = stat["node_name"]
            node_id = stat.get("node_id")
            offline_count = stat["offline_count"]
            total_duration = stat["total_duration"]
            is_offline = node_id in current_offline if node_id else False
            
            box.content(f"▲ {node_name}")
            box.content(f"  OUTAGE  {offline_count}x")
            box.content(f"  TOTAL   {_format_duration(total_duration)}")
            if is_offline:
                box.content(f"  STATE   ■ OFFLINE")
            else:
                box.content(f"  STATE   RECOVERED")
            box.empty()
    
    box.separator()
    box.content(f"ONLINE {online_count}/{total_nodes}")
    if len(nodes_with_events) > 0:
        box.content(f"ERR    {len(nodes_with_events)}")
    box.separator()
    box.content(f"UPD {now.strftime('%H:%M:%S')}")
    box.footer()
    
    return box.build()


def format_daily_archive_card(date_str: str, node_stats: list) -> str:
    """Format end-of-day archive card for Telegram (HTML).
    Terminal-style design with dynamic width alignment.
    """
    now = datetime.now(TZ_BEIJING)
    nodes_with_events = [s for s in node_stats if s["offline_count"] > 0]
    total_nodes = len(node_stats)
    
    total_offline_seconds = sum(s["total_duration"] for s in nodes_with_events)
    total_offline_count = sum(s["offline_count"] for s in nodes_with_events)
    
    # Calculate average uptime
    if total_offline_seconds > 0 and total_nodes > 0:
        avg_uptime = max(0, 100 - (total_offline_seconds / (total_nodes * 864)) * 100)
    else:
        avg_uptime = 100.0
    
    box = TerminalBox()
    box.header("DAILY REPORT")
    box.empty()
    box.content(f"{date_str}  ARCHIVED")
    box.empty()
    box.separator("SUMMARY")
    box.empty()
    box.content(f"TOTAL NODES  {total_nodes}")
    box.content(f"INCIDENTS    {total_offline_count}")
    box.content(f"DOWNTIME     {_format_duration(total_offline_seconds)}")
    box.content(f"AVG UPTIME   {avg_uptime:.1f}%")
    box.empty()
    
    if nodes_with_events:
        box.separator("DETAILS")
        box.empty()
        
        # Sort by total duration (worst first)
        sorted_stats = sorted(nodes_with_events, key=lambda x: x["total_duration"], reverse=True)
        
        for stat in sorted_stats:
            node_name = stat["node_name"]
            offline_count = stat["offline_count"]
            total_duration = stat["total_duration"]
            uptime_pct = max(0, 100 - (total_duration / 864) * 100)
            
            box.content(f"▸ {node_name}")
            box.content(f"  {offline_count}x / {_format_duration(total_duration)} / {uptime_pct:.1f}%")
            box.empty()
    
    box.separator()
    box.content(f"GENERATED {now.strftime('%H:%M:%S')}")
    box.footer()
    
    return box.build()



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


