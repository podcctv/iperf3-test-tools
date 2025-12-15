from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import declarative_base, relationship

from .constants import DEFAULT_IPERF_PORT

Base = declarative_base()


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    ip = Column(String, nullable=False)
    agent_port = Column(Integer, default=8000)
    iperf_port = Column(Integer, default=DEFAULT_IPERF_PORT)
    description = Column(String, nullable=True)
    is_internal = Column(Boolean, default=False)  # True for NAT/internal agents
    # Reverse mode support for NAT agents
    agent_mode = Column(String, default="normal")  # "normal" or "reverse"
    agent_version = Column(String, nullable=True)
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)
    # Whitelist sync status
    whitelist_sync_status = Column(String, default="unknown")  # unknown, synced, failed
    whitelist_sync_message = Column(String, nullable=True)     # Error details or status msg
    whitelist_sync_at = Column(DateTime(timezone=True), nullable=True)
    # Auto-update status
    update_status = Column(String, default="none")  # none, updating, updated, failed
    update_message = Column(String, nullable=True)  # Update details or error message
    update_at = Column(DateTime(timezone=True), nullable=True)  # Last update timestamp

    outgoing_tests = relationship("TestResult", foreign_keys="TestResult.src_node_id", back_populates="src_node")
    incoming_tests = relationship("TestResult", foreign_keys="TestResult.dst_node_id", back_populates="dst_node")


class TestResult(Base):
    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True, index=True)
    src_node_id = Column(Integer, ForeignKey("nodes.id"))
    dst_node_id = Column(Integer, ForeignKey("nodes.id"))
    protocol = Column(String)
    params = Column(JSON)
    raw_result = Column(JSON)
    summary = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    src_node = relationship("Node", foreign_keys=[src_node_id], back_populates="outgoing_tests")
    dst_node = relationship("Node", foreign_keys=[dst_node_id], back_populates="incoming_tests")


class TestSchedule(Base):
    __tablename__ = "test_schedules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    src_node_id = Column(Integer, ForeignKey("nodes.id"))
    dst_node_id = Column(Integer, ForeignKey("nodes.id"))
    protocol = Column(String, default="tcp")
    duration = Column(Integer, default=10)
    parallel = Column(Integer, default=1)
    port = Column(Integer, default=DEFAULT_IPERF_PORT)
    interval_seconds = Column(Integer, nullable=True)  # Legacy, nullable for cron migration
    cron_expression = Column(String, nullable=True)    # Cron format: "*/5 * * * *"
    direction = Column(String, default="upload")  # upload, download, bidirectional
    udp_bandwidth = Column(String, nullable=True)  # UDP bandwidth (e.g., "100M", "1G")
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    schedule_synced_at = Column(DateTime(timezone=True), nullable=True)  # Last sync to agent
    notes = Column(String, nullable=True)

    src_node = relationship("Node", foreign_keys=[src_node_id])
    dst_node = relationship("Node", foreign_keys=[dst_node_id])


class ScheduleResult(Base):
    __tablename__ = "schedule_results"
    
    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(Integer, ForeignKey("test_schedules.id"))
    test_result_id = Column(Integer, ForeignKey("test_results.id"), nullable=True)
    executed_at = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String, default="success")
    error_message = Column(String, nullable=True)
    
    schedule = relationship("TestSchedule", foreign_keys=[schedule_id])
    test_result = relationship("TestResult", foreign_keys=[test_result_id])


class PendingTask(Base):
    """Task queue for reverse mode (NAT) agents that cannot be reached directly."""
    __tablename__ = "pending_tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    node_name = Column(String, nullable=False, index=True)  # Target agent node name
    task_type = Column(String, nullable=False)  # "iperf_test", "streaming_probe"
    task_data = Column(JSON, nullable=False)  # Task parameters
    schedule_id = Column(Integer, ForeignKey("test_schedules.id"), nullable=True)  # Optional link to schedule
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    claimed_at = Column(DateTime(timezone=True), nullable=True)  # When agent picked up the task
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, default="pending")  # pending, claimed, completed, failed, expired
    result_data = Column(JSON, nullable=True)  # Task result when completed
    error_message = Column(Text, nullable=True)
    
    schedule = relationship("TestSchedule", foreign_keys=[schedule_id])


# ============== Traceroute Scheduled Monitoring ==============

class TraceSchedule(Base):
    """Scheduled traceroute monitoring tasks."""
    __tablename__ = "trace_schedules"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    src_node_id = Column(Integer, ForeignKey("nodes.id"), nullable=False)
    target_type = Column(String, default="custom")  # "custom" or "node"
    target_address = Column(String, nullable=True)  # IP or hostname for custom
    target_node_id = Column(Integer, ForeignKey("nodes.id"), nullable=True)  # for node type
    
    interval_seconds = Column(Integer, nullable=False, default=3600)  # Default 1 hour
    max_hops = Column(Integer, default=30)
    enabled = Column(Boolean, default=True)
    
    # Alert settings
    alert_on_change = Column(Boolean, default=True)  # Alert when route changes
    alert_threshold = Column(Integer, default=1)  # Number of hop changes to trigger alert
    alert_channels = Column(JSON, default=list)  # ["bell", "telegram"]
    
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    src_node = relationship("Node", foreign_keys=[src_node_id])
    target_node = relationship("Node", foreign_keys=[target_node_id])


class TraceResult(Base):
    """Traceroute execution results."""
    __tablename__ = "trace_results"
    
    id = Column(Integer, primary_key=True, index=True)
    schedule_id = Column(Integer, ForeignKey("trace_schedules.id"), nullable=True)  # Null for ad-hoc traces
    src_node_id = Column(Integer, ForeignKey("nodes.id"), nullable=False)
    target = Column(String, nullable=False)  # Target IP/hostname
    
    executed_at = Column(DateTime(timezone=True), server_default=func.now())
    total_hops = Column(Integer)
    hops = Column(JSON)  # List of hop details
    route_hash = Column(String)  # For quick comparison
    tool_used = Column(String)  # "mtr" or "traceroute"
    elapsed_ms = Column(Integer)
    
    # Route change detection
    has_change = Column(Boolean, default=False)  # True if route changed from previous
    change_summary = Column(JSON, nullable=True)  # Details of what changed
    previous_route_hash = Column(String, nullable=True)  # For comparison reference
    source_type = Column(String, default="scheduled")  # "scheduled", "single", "multisrc"
    
    schedule = relationship("TraceSchedule", foreign_keys=[schedule_id])
    src_node = relationship("Node", foreign_keys=[src_node_id])


# ============== ASN Cache for PeeringDB Data ==============

class AsnCache(Base):
    """Cached ASN information from PeeringDB for Tier classification."""
    __tablename__ = "asn_cache"
    
    asn = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    info_type = Column(String, nullable=True)  # NSP, Content, Cable/DSL/ISP, etc.
    info_scope = Column(String, nullable=True)  # Global, Regional, etc.
    ix_count = Column(Integer, default=0)
    tier = Column(String, nullable=True)  # T1, T2, T3, IX, CDN, ISP
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


# ============== Audit Log ==============

class AuditLog(Base):
    """Audit log for tracking important operations."""
    __tablename__ = "audit_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    action = Column(String, nullable=False, index=True)  # login, logout, node_create, etc.
    actor_ip = Column(String, nullable=True)  # Client IP address
    actor_type = Column(String, default="user")  # user, system, agent
    resource_type = Column(String, nullable=True)  # node, schedule, config, etc.
    resource_id = Column(String, nullable=True)  # ID of affected resource
    details = Column(JSON, nullable=True)  # Additional context
    success = Column(Boolean, default=True)  # Whether action succeeded
