from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from .constants import DEFAULT_IPERF_PORT


class NodeBase(BaseModel):
    name: str
    ip: str
    agent_port: int = Field(default=8000, ge=1, le=65535)
    iperf_port: int = Field(default=DEFAULT_IPERF_PORT, ge=1, le=65535)
    description: Optional[str] = None


class NodeCreate(NodeBase):
    pass


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    ip: Optional[str] = None
    agent_port: Optional[int] = Field(default=None, ge=1, le=65535)
    iperf_port: Optional[int] = Field(default=None, ge=1, le=65535)
    description: Optional[str] = None


class NodeRead(NodeBase):
    id: int

    class Config:
        from_attributes = True


class BackboneLatency(BaseModel):
    key: str
    name: str
    host: str
    port: int
    latency_ms: float | None = None
    status: str = "unknown"
    detail: str | None = None
    checked_at: int | None = None


class StreamingServiceStatus(BaseModel):
    service: str
    unlocked: bool
    key: Optional[str] = None
    status_code: Optional[int] = None
    detail: Optional[str] = None


class NodeWithStatus(NodeRead):
    status: str
    server_running: bool | None = None
    health_timestamp: int | None = None
    checked_at: int | None = None
    detected_iperf_port: int | None = None
    backbone_latency: list[BackboneLatency] | None = None
    streaming: list[StreamingServiceStatus] | None = None
    streaming_checked_at: int | None = None


class TestCreate(BaseModel):
    src_node_id: int
    dst_node_id: int
    protocol: str = "tcp"
    duration: int = Field(default=10, gt=0)
    parallel: int = Field(default=1, gt=0)
    port: int = Field(default=DEFAULT_IPERF_PORT, ge=1, le=65535)
    reverse: bool = False


class TestRead(BaseModel):
    id: int
    src_node_id: int
    dst_node_id: int
    protocol: str
    params: Any
    raw_result: Any
    summary: Any | None = None
    created_at: datetime | None = None

    class Config:
        from_attributes = True


class TestScheduleBase(BaseModel):
    name: str
    src_node_id: int
    dst_node_id: int
    protocol: str = "tcp"
    duration: int = Field(default=10, gt=0)
    parallel: int = Field(default=1, gt=0)
    port: int = Field(default=DEFAULT_IPERF_PORT, ge=1, le=65535)
    interval_seconds: int = Field(default=3600, gt=0)
    enabled: bool = True
    notes: Optional[str] = None


class TestScheduleCreate(TestScheduleBase):
    pass


class TestScheduleUpdate(BaseModel):
    name: Optional[str] = None
    src_node_id: Optional[int] = None
    dst_node_id: Optional[int] = None
    protocol: Optional[str] = None
    duration: Optional[int] = Field(default=None, gt=0)
    parallel: Optional[int] = Field(default=None, gt=0)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    interval_seconds: Optional[int] = Field(default=None, gt=0)
    enabled: Optional[bool] = None
    notes: Optional[str] = None


class TestScheduleRead(TestScheduleBase):
    id: int
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class StreamingTestResult(BaseModel):
    node_id: int
    node_name: str
    services: List[StreamingServiceStatus]
    elapsed_ms: Optional[int] = None


class AgentConfigBase(BaseModel):
    name: str
    host: str
    agent_port: int = Field(default=8000, ge=1, le=65535)
    iperf_port: int = Field(default=DEFAULT_IPERF_PORT, ge=1, le=65535)
    ssh_port: Optional[int] = Field(default=None, ge=1, le=65535)
    image: str = "iperf-agent:latest"
    container_name: str = "iperf-agent"
    description: Optional[str] = None


class AgentConfigCreate(AgentConfigBase):
    pass


class AgentConfigUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    agent_port: Optional[int] = Field(default=None, ge=1, le=65535)
    iperf_port: Optional[int] = Field(default=None, ge=1, le=65535)
    ssh_port: Optional[int] = Field(default=None, ge=1, le=65535)
    image: Optional[str] = None
    container_name: Optional[str] = None
    description: Optional[str] = None


class AgentConfigRead(AgentConfigBase):
    pass


class AgentActionResult(BaseModel):
    status: str
    message: Optional[str] = None
    logs: Optional[str] = None
