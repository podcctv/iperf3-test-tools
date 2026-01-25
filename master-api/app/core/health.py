import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import httpx
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import Node, AlertConfig, OfflineMessage, OfflineEvent, DailyStatsMessage
from app.schemas import NodeWithStatus, BackboneLatency, StreamingServiceStatus
from app import alert_service

logger = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

ZHEJIANG_TARGETS = [
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

# ============================================================================
# Backbone Latency Monitor
# ============================================================================

class BackboneLatencyMonitor:
    def __init__(self, targets: List[dict], interval_seconds: int = 60) -> None:
        self.targets = targets
        self.interval_seconds = interval_seconds
        self._cache: Dict[str, BackboneLatency] = {}
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task:
            return
        await self.refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Failed to refresh backbone latency")
            await asyncio.sleep(self.interval_seconds)

    async def _measure_target(self, target: dict) -> BackboneLatency:
        samples: list[float] = []
        detail: str | None = None
        for _ in range(2):
            start = time.perf_counter()
            try:
                # Use open_connection for a simple TCP connect check
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(target["host"], int(target["port"])),
                    timeout=5,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                samples.append((time.perf_counter() - start) * 1000)
            except Exception as exc:  # pragma: no cover - network dependent
                detail = str(exc)

        latency_ms = sum(samples) / len(samples) if samples else None
        checked_at = int(datetime.now(timezone.utc).timestamp())
        return BackboneLatency(
            key=target["key"],
            name=target["name"],
            host=target["host"],
            port=int(target["port"]),
            latency_ms=round(latency_ms, 2) if latency_ms is not None else None,
            status="ok" if latency_ms is not None else "error",
            detail=None if latency_ms is not None else detail,
            checked_at=checked_at,
        )

    async def refresh(self) -> List[BackboneLatency]:
        async with self._lock:
            # Measure all targets in parallel
            results = await asyncio.gather(
                *[self._measure_target(target) for target in self.targets]
            )
            self._cache = {result.key: result for result in results}
            return results

    async def get_statuses(self) -> List[BackboneLatency]:
        if not self._cache:
            return await self.refresh()
        # Return results in the same order as targets
        ordered = [self._cache[target["key"]] for target in self.targets if target["key"] in self._cache]
        return ordered


# ============================================================================
# Node Health Check Helper
# ============================================================================

async def _check_node_health(node: Node) -> NodeWithStatus:
    """Check health of a single node (HTTP or heartbeat)."""
    checked_at = int(datetime.now(timezone.utc).timestamp())
    
    # For NAT/reverse mode nodes, check heartbeat instead of HTTP
    node_mode = getattr(node, "agent_mode", "normal") or "normal"
    
    # Debug: Print to stdout for guaranteed visibility
    last_heartbeat = getattr(node, "last_heartbeat", None)
    # print(f"[HEALTH] Checking {node.name}: agent_mode='{node_mode}', last_heartbeat={last_heartbeat}", flush=True)
    
    if node_mode == "reverse":
        agent_version = getattr(node, "agent_version", None)
        
        if last_heartbeat:
            # Handle timezone-aware vs naive datetime comparison
            now = datetime.now(timezone.utc)
            if last_heartbeat.tzinfo is None:
                # Naive datetime from SQLite - assume it's UTC
                last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
            
            heartbeat_age = (now - last_heartbeat).total_seconds()
            # logger.info(f"[HEALTH] {node.name}: heartbeat_age={heartbeat_age:.1f}s (threshold=60s)")
            
            # Check if heartbeat is within 60 seconds (online threshold)
            if heartbeat_age < 60:
                # logger.info(f"[HEALTH] {node.name}: ONLINE (reverse mode, heartbeat fresh)")
                return NodeWithStatus(
                    id=node.id,
                    name=node.name,
                    ip=node.ip,
                    agent_port=node.agent_port,
                    description=node.description,
                    iperf_port=node.iperf_port,
                    status="online",
                    server_running=True,  # Assume server is running for reverse agents
                    health_timestamp=int(last_heartbeat.timestamp()),
                    checked_at=checked_at,
                    detected_iperf_port=node.iperf_port,
                    detected_agent_port=node.agent_port,
                    backbone_latency=None,
                    streaming=None,
                    streaming_checked_at=None,
                    whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
                    whitelist_sync_message=getattr(node, "whitelist_sync_message", None),
                    whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
                    agent_version=agent_version,
                    agent_mode=node_mode,
                )
        
        # NAT node is offline (no recent heartbeat)
        return NodeWithStatus(
            id=node.id,
            name=node.name,
            ip=node.ip,
            agent_port=node.agent_port,
            iperf_port=node.iperf_port,
            description=node.description,
            status="offline",
            server_running=None,
            health_timestamp=int(last_heartbeat.timestamp()) if last_heartbeat else None,
            checked_at=checked_at,
            detected_iperf_port=None,
            detected_agent_port=None,
            whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
            whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
            agent_version=agent_version,
            agent_mode=node_mode,
        )
    
    # Normal nodes - check via HTTP health endpoint
    url = f"http://{node.ip}:{node.agent_port}/health"
    try:
        # Create a new client for each request to avoid event loop issues across threads/tasks
        # In a high-throughput scenario, we should use a shared client, but here it's occasional.
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                detected_port = data.get("port")
                latency_payload = data.get("backbone_latency") or []
                streaming_payload = data.get("streaming") or []
                streaming_checked_at = data.get("streaming_checked_at")
                backbone_latency = [
                    BackboneLatency(**item) for item in latency_payload if isinstance(item, dict)
                ]
                streaming_statuses = [
                    StreamingServiceStatus(**item)
                    for item in streaming_payload
                    if isinstance(item, dict)
                ]
                return NodeWithStatus(
                    id=node.id,
                    name=node.name,
                    ip=node.ip,
                    agent_port=node.agent_port,
                    description=node.description,
                    iperf_port=node.iperf_port,
                    status="online",
                    server_running=bool(data.get("server_running")),
                    health_timestamp=data.get("timestamp"),
                    checked_at=checked_at,
                    detected_iperf_port=int(detected_port) if detected_port else None,
                    detected_agent_port=node.agent_port,  # Agent port is the port we connected to
                    backbone_latency=backbone_latency or None,
                    streaming=streaming_statuses or None,
                    streaming_checked_at=streaming_checked_at,
                    whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
                    whitelist_sync_message=getattr(node, "whitelist_sync_message", None),
                    whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
                    agent_version=data.get("version"),
                    agent_mode=node_mode,
                )
    except Exception:
        pass

    # Fallback: Node is offline or unreachable
    return NodeWithStatus(
        id=node.id,
        name=node.name,
        ip=node.ip,
        agent_port=node.agent_port,
        iperf_port=node.iperf_port,
        description=node.description,
        status="offline",
        server_running=None,
        health_timestamp=None,
        checked_at=checked_at,
        whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
        whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
        agent_mode=node_mode,
    )


# ============================================================================
# Node Health Monitor
# ============================================================================

class NodeHealthMonitor:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._cache: Dict[int, NodeWithStatus] = {}
        self._lock = asyncio.Lock()

    def invalidate(self, node_id: int | None = None) -> None:
        """Clear cached health state so updates reflect immediately."""
        if node_id is None:
            self._cache = {}
        else:
            self._cache.pop(node_id, None)

    async def start(self) -> None:
        if self._task:
            return
        # Initial refresh
        await self.refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        ping_cycle_counter = 0  # Track cycles for ping storage (every 2 = 60s at 30s interval)
        while True:
            try:
                statuses = await self.refresh()
                await self._sync_ports(statuses)
                await self._check_alerts(statuses)  # Check for alerts
                
                # Store ping history every 60 seconds (every 2 cycles at 30s interval)
                ping_cycle_counter += 1
                if ping_cycle_counter >= 2:
                    ping_cycle_counter = 0
                    await self._store_ping_history(statuses)
                    await self._cleanup_old_pings()
            except Exception:
                logger.exception("Failed to refresh node health")
            await asyncio.sleep(self.interval_seconds)

    async def refresh(self, nodes: List[Node] | None = None) -> List[NodeWithStatus]:
        async with self._lock:
            if nodes is None:
                db = SessionLocal()
                try:
                    nodes = db.scalars(select(Node)).all()
                finally:
                    db.close()

            statuses = await asyncio.gather(*[_check_node_health(node) for node in nodes])
            now_ts = int(datetime.now(timezone.utc).timestamp())
            for status in statuses:
                status.checked_at = now_ts
            self._cache = {status.id: status for status in statuses}
            return statuses

    async def get_statuses(self) -> List[NodeWithStatus]:
        db = SessionLocal()
        try:
            nodes = db.scalars(select(Node)).all()
        finally:
            db.close()

        cached_ids = set(self._cache.keys())
        current_ids = {node.id for node in nodes}
        # If cache mismatch or empty, force refresh
        if not self._cache or cached_ids != current_ids:
            return await self.refresh(nodes)
        return list(self._cache.values())

    async def check_node(self, node: Node) -> NodeWithStatus:
        status = await _check_node_health(node)
        status.checked_at = int(datetime.now(timezone.utc).timestamp())
        self._cache[node.id] = status
        return status
        
    async def _sync_ports(self, statuses: List[NodeWithStatus]) -> None:
        """Update node database if detected ports differ from config."""
        db = SessionLocal()
        try:
            updates_made = False
            for status in statuses:
                if status.status != "online":
                    continue
                    
                # Check iperf port difference
                if status.detected_iperf_port and status.detected_iperf_port != status.iperf_port:
                    node = db.get(Node, status.id)
                    if node:
                        node.iperf_port = status.detected_iperf_port
                        updates_made = True
                        logger.info(f"Auto-updating iperf port for node {node.name}: {status.detected_iperf_port}")
            
            if updates_made:
                db.commit()
        except Exception as e:
            logger.error(f"Failed to sync ports: {e}")
        finally:
            db.close()

    async def _check_alerts(self, statuses: List[NodeWithStatus]) -> None:
        """Check for alert conditions and trigger notifications."""
        
        # GMT+8 Beijing timezone
        TZ_BEIJING = timezone(timedelta(hours=8))
        
        db = SessionLocal()
        try:
            thresholds_config = db.query(AlertConfig).filter(AlertConfig.key == "thresholds").first()
            telegram_config = db.query(AlertConfig).filter(AlertConfig.key == "telegram").first()
            
            # Default thresholds
            ping_threshold_ms = 500
            
            if thresholds_config and thresholds_config.value:
                ping_threshold_ms = thresholds_config.value.get("ping_high_ms", 500)
            
            # Check if telegram notifications are enabled
            notify_node_offline = False
            notify_ping_high = False
            bot_token = None
            chat_id = None
            
            if telegram_config and telegram_config.enabled and telegram_config.value:
                notify_node_offline = telegram_config.value.get("notify_node_offline", False)
                notify_ping_high = telegram_config.value.get("notify_ping_high", False)
                bot_token = telegram_config.value.get("bot_token")
                chat_id = telegram_config.value.get("chat_id")
            
            # Node filtering
            node_scope = "all"
            selected_nodes = set()
            if thresholds_config and thresholds_config.value:
                node_scope = thresholds_config.value.get("node_scope", "all")
                selected_nodes = set(thresholds_config.value.get("selected_nodes", []))
            
            # Today's date in Beijing timezone
            now_beijing = datetime.now(TZ_BEIJING)
            today_str = now_beijing.strftime("%Y-%m-%d")
            
            # Track current offline nodes for stats card
            current_offline_durations = {}  # {node_id: seconds_offline}
            
            for status in statuses:
                # Skip nodes not in selection (if using selected mode)
                if node_scope == "selected" and status.id not in selected_nodes:
                    continue
                
                # Handle offline nodes with card system
                if status.status == "offline":
                    # Check if we already have an offline message for this node
                    existing_msg = db.query(OfflineMessage).filter(
                        OfflineMessage.node_id == status.id
                    ).first()
                    
                    if existing_msg:
                        # Calculate current offline duration
                        duration = (datetime.now(timezone.utc) - existing_msg.offline_since).total_seconds()
                        current_offline_durations[status.id] = duration
                        
                        if notify_node_offline and bot_token and chat_id:
                            # Update existing card with new duration
                            await alert_service.edit_offline_card(
                                bot_token=bot_token,
                                chat_id=existing_msg.chat_id,
                                message_id=existing_msg.message_id,
                                node_name=status.name,
                                node_ip=status.ip,
                                offline_since=existing_msg.offline_since
                            )
                            # Update last_updated timestamp
                            existing_msg.last_updated = datetime.now(timezone.utc)
                            db.commit()
                    else:
                        # New offline event - create records
                        offline_since = datetime.now(timezone.utc)
                        current_offline_durations[status.id] = 0
                        
                        # Create OfflineEvent record
                        offline_event = OfflineEvent(
                            node_id=status.id,
                            node_name=status.name,
                            started_at=offline_since,
                            date=today_str
                        )
                        db.add(offline_event)
                        db.commit()
                        logger.info(f"Created offline event for node {status.name}")
                        
                        if notify_node_offline and bot_token and chat_id:
                            # Send new offline card
                            message_id = await alert_service.send_offline_card(
                                bot_token=bot_token,
                                chat_id=chat_id,
                                node_name=status.name,
                                node_ip=status.ip,
                                offline_since=offline_since
                            )
                            
                            if message_id:
                                # Save offline message record
                                offline_msg = OfflineMessage(
                                    node_id=status.id,
                                    message_id=message_id,
                                    chat_id=chat_id,
                                    offline_since=offline_since
                                )
                                db.add(offline_msg)
                                db.commit()
                                logger.info(f"Created offline message record for node {status.name}")
                
                # Handle online nodes - delete any existing offline card and close event
                elif status.status == "online":
                    existing_msg = db.query(OfflineMessage).filter(
                        OfflineMessage.node_id == status.id
                    ).first()
                    
                    if existing_msg:
                        # Close the OfflineEvent
                        open_event = db.query(OfflineEvent).filter(
                            OfflineEvent.node_id == status.id,
                            OfflineEvent.ended_at == None
                        ).first()
                        
                        if open_event:
                            open_event.ended_at = datetime.now(timezone.utc)
                            open_event.duration_seconds = int(
                                (open_event.ended_at - open_event.started_at).total_seconds()
                            )
                            db.commit()
                            logger.info(f"Closed offline event for node {status.name}, duration={open_event.duration_seconds}s")
                        
                        if bot_token:
                            # Delete the offline card from Telegram
                            await alert_service.delete_telegram_message(
                                bot_token=bot_token,
                                chat_id=existing_msg.chat_id,
                                message_id=existing_msg.message_id
                            )
                        
                        # Remove from database
                        db.delete(existing_msg)
                        db.commit()
                        logger.info(f"Deleted offline message for node {status.name} (now online)")
                
                # Check for high ping latency (keep existing behavior)
                if status.backbone_latency and notify_ping_high:
                    for lat in status.backbone_latency:
                        if lat.latency_ms and lat.latency_ms > ping_threshold_ms:
                            # Note: trigger_alert function is local in main.py, we need to import or reimplement it
                            # For now, let's just log it or rely on alert_service later
                            # The original code called 'trigger_alert' which is a standalone function in main.py
                            # We should probably move trigger_alert to alert_service.py or similar
                            logger.warning(f"High ping detected for {status.name}: {lat.latency_ms}ms > {ping_threshold_ms}ms")
                            pass 

            # ========== Daily Statistics Card ==========
            if notify_node_offline and bot_token and chat_id:
                # Get all nodes for stats
                all_nodes = db.query(Node).all()
                
                # Calculate daily stats for each node
                node_stats = []
                for node in all_nodes:
                    # Skip if using selected mode and node not selected
                    if node_scope == "selected" and node.id not in selected_nodes:
                        continue
                    
                    # Query offline events for today
                    events = db.query(OfflineEvent).filter(
                        OfflineEvent.node_id == node.id,
                        OfflineEvent.date == today_str
                    ).all()
                    
                    offline_count = len(events)
                    total_duration = 0
                    
                    for event in events:
                        if event.duration_seconds:
                            total_duration += event.duration_seconds
                        elif event.ended_at is None:
                            # Still offline - calculate current duration
                            total_duration += int((datetime.now(timezone.utc) - event.started_at).total_seconds())
                    
                    node_stats.append({
                        "node_id": node.id,
                        "node_name": node.name,
                        "offline_count": offline_count,
                        "total_duration": total_duration
                    })
                
                # Check for existing daily stats message
                stats_msg = db.query(DailyStatsMessage).filter(
                    DailyStatsMessage.date == today_str
                ).first()
                
                # Check if there's a message from a previous day (date changed)
                old_stats_msg = db.query(DailyStatsMessage).filter(
                    DailyStatsMessage.date != today_str
                ).first()
                
                if old_stats_msg:
                    # Date has changed - archive the old day
                    old_date = old_stats_msg.date
                    
                    # Calculate stats for the old day (for archive)
                    old_node_stats = []
                    for node in all_nodes:
                        if node_scope == "selected" and node.id not in selected_nodes:
                            continue
                        events = db.query(OfflineEvent).filter(
                            OfflineEvent.node_id == node.id,
                            OfflineEvent.date == old_date
                        ).all()
                        offline_count = len(events)
                        total_duration = sum(e.duration_seconds or 0 for e in events)
                        old_node_stats.append({
                            "node_id": node.id,
                            "node_name": node.name,
                            "offline_count": offline_count,
                            "total_duration": total_duration
                        })
                    
                    # Send archive card (this persists in chat)
                    await alert_service.send_daily_archive_card(
                        bot_token=bot_token,
                        chat_id=old_stats_msg.chat_id,
                        date_str=old_date,
                        node_stats=old_node_stats
                    )
                    logger.info(f"Sent daily archive for {old_date}")
                    
                    # Delete the old live stats message from Telegram
                    await alert_service.delete_telegram_message(
                        bot_token=bot_token,
                        chat_id=old_stats_msg.chat_id,
                        message_id=old_stats_msg.message_id
                    )
                    
                    # Remove old record from database
                    db.delete(old_stats_msg)
                    db.commit()
                    logger.info(f"Deleted old live stats card for {old_date}")
                
                if stats_msg:
                    # Update existing card
                    await alert_service.edit_daily_stats_card(
                        bot_token=bot_token,
                        chat_id=stats_msg.chat_id,
                        message_id=stats_msg.message_id,
                        date_str=today_str,
                        node_stats=node_stats,
                        current_offline=current_offline_durations
                    )
                    stats_msg.last_updated = datetime.now(timezone.utc)
                    db.commit()
                else:
                    # Send new daily stats card
                    message_id = await alert_service.send_daily_stats_card(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        date_str=today_str,
                        node_stats=node_stats,
                        current_offline=current_offline_durations
                    )
                    
                    if message_id:
                        # Save daily stats message record
                        daily_msg = DailyStatsMessage(
                            message_id=message_id,
                            chat_id=chat_id,
                            date=today_str,
                            last_updated=datetime.now(timezone.utc)
                        )
                        db.add(daily_msg)
                        db.commit()
                        logger.info(f"Created daily stats message record for {today_str}")

        except Exception as e:
            logger.error(f"Error checking alerts: {e}")
        finally:
            db.close()

    async def _store_ping_history(self, statuses: List[NodeWithStatus]) -> None:
        """Store ping latency history for graph display."""
        import json
        from app.redis_client import get_redis_client
        
        try:
            redis = get_redis_client()
            if not redis:
                return

            pipe = redis.pipeline()
            now_ts = int(time.time())
            count = 0
            
            for status in statuses:
                if not status.backbone_latency:
                    continue
                
                # Format: node_id:timestamp -> json_data
                # We use a sorted set for time-series: key=node:ping:history, score=timestamp, member=json_data
                key = f"node:{status.id}:ping_history"
                
                # Create record
                record = {
                    "ts": now_ts,
                    "data": [
                        {"name": lat.name, "ms": lat.latency_ms} 
                        for lat in status.backbone_latency 
                        if lat.latency_ms is not None
                    ]
                }
                
                if not record["data"]:
                    # print(f"[PING] Node {status.id} ({status.name}): no backbone_latency", flush=True)
                    continue
                
                # Add to sorted set
                # ZADD key score member
                # Note: member must be string
                data_str = json.dumps(record)
                pipe.zadd(key, {data_str: now_ts})
                
                # Trim old records (keep last 24 hours = 24 * 60 = 1440 records at 1/min)
                # ZREMRANGEBYRANK key 0 -1441
                pipe.zremrangebyrank(key, 0, -1441)
                
                # Set expiry (25 hours)
                pipe.expire(key, 25 * 3600)
                
                count += 1
            
            if count > 0:
                await pipe.execute()
                # print(f"[PING] Stored {count} ping records", flush=True)
                
        except Exception as e:
            logger.error(f"Failed to store ping history: {e}")

    async def _cleanup_old_pings(self) -> None:
        pass
