from sqlalchemy import Column, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    ip = Column(String, nullable=False)
    agent_port = Column(Integer, default=8000)
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

    src_node = relationship("Node", foreign_keys=[src_node_id], back_populates="outgoing_tests")
    dst_node = relationship("Node", foreign_keys=[dst_node_id], back_populates="incoming_tests")
