import asyncio
from typing import List

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import engine, get_db
from .models import Base, Node, TestResult
from .schemas import NodeCreate, NodeRead, NodeWithStatus, TestCreate, TestRead

Base.metadata.create_all(bind=engine)

app = FastAPI(title="iperf3 master api")


@app.get("/")
def root() -> dict:
    """Provide a simple landing response instead of a 404."""

    return {
        "message": "iperf3 master api",
        "docs_url": "/docs",
        "health_url": "/health",
    }


async def _check_node_health(node: Node) -> NodeWithStatus:
    url = f"http://{node.ip}:{node.agent_port}/health"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                return NodeWithStatus(
                    id=node.id,
                    name=node.name,
                    ip=node.ip,
                    agent_port=node.agent_port,
                    description=node.description,
                    status="online",
                    server_running=bool(data.get("server_running")),
                    health_timestamp=data.get("timestamp"),
                )
    except Exception:
        pass

    return NodeWithStatus(
        id=node.id,
        name=node.name,
        ip=node.ip,
        agent_port=node.agent_port,
        description=node.description,
        status="offline",
        server_running=None,
        health_timestamp=None,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/nodes", response_model=NodeRead)
def create_node(node: NodeCreate, db: Session = Depends(get_db)):
    obj = Node(
        name=node.name,
        ip=node.ip,
        agent_port=node.agent_port,
        description=node.description,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/nodes", response_model=List[NodeRead])
def list_nodes(db: Session = Depends(get_db)):
    nodes = db.scalars(select(Node)).all()
    return nodes


@app.get("/nodes/status", response_model=List[NodeWithStatus])
async def nodes_with_status(db: Session = Depends(get_db)):
    nodes = db.scalars(select(Node)).all()
    results = await asyncio.gather(*[_check_node_health(node) for node in nodes])
    return results


@app.post("/tests", response_model=TestRead)
async def create_test(test: TestCreate, db: Session = Depends(get_db)):
    src = db.get(Node, test.src_node_id)
    dst = db.get(Node, test.dst_node_id)
    if not src or not dst:
        raise HTTPException(status_code=404, detail="node not found")

    agent_url = f"http://{src.ip}:{src.agent_port}/run_test"
    payload = {
        "target": dst.ip,
        "port": test.port,
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel,
    }

    async with httpx.AsyncClient(timeout=test.duration + settings.request_timeout) as client:
        response = await client.post(agent_url, json=payload)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)

    raw_data = response.json()
    obj = TestResult(
        src_node_id=src.id,
        dst_node_id=dst.id,
        protocol=test.protocol,
        params=payload,
        raw_result=raw_data,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/tests", response_model=List[TestRead])
def list_tests(db: Session = Depends(get_db)):
    results = db.scalars(select(TestResult)).all()
    return results
