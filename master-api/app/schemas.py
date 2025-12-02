from typing import Any, List, Optional

from pydantic import BaseModel, Field


class NodeBase(BaseModel):
    name: str
    ip: str
    agent_port: int = Field(default=8000, ge=1, le=65535)
    description: Optional[str] = None


class NodeCreate(NodeBase):
    pass


class NodeRead(NodeBase):
    id: int

    class Config:
        orm_mode = True


class NodeWithStatus(NodeRead):
    status: str
    server_running: bool | None = None
    health_timestamp: int | None = None


class TestCreate(BaseModel):
    src_node_id: int
    dst_node_id: int
    protocol: str = "tcp"
    duration: int = Field(default=10, gt=0)
    parallel: int = Field(default=1, gt=0)
    port: int = Field(default=5201, ge=1, le=65535)


class TestRead(BaseModel):
    id: int
    src_node_id: int
    dst_node_id: int
    protocol: str
    params: Any
    raw_result: Any

    class Config:
        orm_mode = True
