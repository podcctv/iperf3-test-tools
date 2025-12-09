from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, func
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
    interval_seconds = Column(Integer, nullable=False)
    direction = Column(String, default="upload")  # upload, download, bidirectional
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True)
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
