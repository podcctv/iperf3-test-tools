## 一、总体目标 & 架构思路

你的需求拆开其实是三件事：

1. **在各个服务器上，通过脚本一键部署 iperf3 服务**（最好用 Docker，指定好端口）
2. **有一台 “master” 主机，用 Docker 部署 web 管理端**
3. **在 web 里：**

   * 管理测试节点（哪些服务器、开放哪些端口）
   * 下发指令：指定 A→B、A→B,C 做 TCP/UDP 速率测试
   * 图形化展示测试结果（列表 + 折线图）

我给你的方案是“**Master + Agent**”模式：

* 每台被测服务器上跑一个 `iperf-agent` 容器：

  * 内部带 `iperf3`
  * 提供一个轻量 HTTP API（Flask）供 master 调用
  * 本地启动 / 停止 iperf3 server，或者本地作为 client 对其它节点跑测试
* master 上用 `docker-compose` 跑一套：

  * `master-api`（FastAPI）
  * `master-web`（前端）
  * `db`（PostgreSQL 或 SQLite）
* master 只负责：存 node 列表、发 HTTP 请求给 agent、收结果入库、前端展示。

数据面：iperf3 流量在 **各测试节点之间直接跑**，不经过 master；
控制面：master → agent 走 HTTP，就几 KB 的 JSON。

---

## 二、Agent 设计：每台服务器部署的 iperf 节点

### 1. Agent 功能

部署到每台测试服务器上的 `iperf-agent` 容器负责：

* 启动/停止 iperf3 server：

  * 默认监听 `5201/tcp, 5201/udp`（也可以支持自定义端口）
* 接受 master 发来的 HTTP 命令，执行：

  * `POST /run_test`：作为 client 对目标 IP/端口发起 TCP/UDP 测试
* 将 iperf3 输出解析成 JSON 返回给 master。

### 2. Agent Dockerfile 示例

```dockerfile
# iperf-agent/Dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y iperf3 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py /app/

EXPOSE 8000 5201/tcp 5201/udp

CMD ["python", "agent.py"]
```

`requirements.txt`:

```text
flask
```

### 3. agent.py 示例（Flask + subprocess 调用 iperf3）

```python
from flask import Flask, request, jsonify
import subprocess
import shlex
import threading

app = Flask(__name__)

IPERF_PORT = 5201

# 后台启动 iperf3 server
server_process = None
server_lock = threading.Lock()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/start_server", methods=["POST"])
def start_server():
    global server_process
    with server_lock:
        if server_process and server_process.poll() is None:
            return jsonify({"status": "running"}), 200

        cmd = f"iperf3 -s -p {IPERF_PORT}"
        server_process = subprocess.Popen(
            shlex.split(cmd),
        )
        return jsonify({"status": "started", "port": IPERF_PORT})


@app.route("/stop_server", methods=["POST"])
def stop_server():
    global server_process
    with server_lock:
        if server_process and server_process.poll() is None:
            server_process.terminate()
            server_process = None
            return jsonify({"status": "stopped"})
        return jsonify({"status": "not_running"}), 200


@app.route("/run_test", methods=["POST"])
def run_test():
    """
    body 示例:
    {
      "target": "10.0.0.12",
      "port": 5201,
      "duration": 10,
      "protocol": "tcp",   # or "udp"
      "parallel": 4
    }
    """
    data = request.get_json(force=True)
    target = data["target"]
    port = data.get("port", IPERF_PORT)
    duration = int(data.get("duration", 10))
    protocol = data.get("protocol", "tcp")
    parallel = int(data.get("parallel", 1))

    proto_flag = "-u" if protocol == "udp" else ""
    cmd = f"iperf3 -c {target} -p {port} -t {duration} -P {parallel} {proto_flag} -J"

    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=duration + 10
        )
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "error": "timeout"}), 500

    if result.returncode != 0:
        return jsonify({"status": "error", "error": result.stderr}), 500

    # iperf3 -J 原生输出就是 JSON
    import json
    try:
        data = json.loads(result.stdout)
    except Exception:
        return jsonify({"status": "error", "error": "parse_failed"}), 500

    return jsonify({"status": "ok", "iperf_result": data})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
```

---

## 三、Agent 一键部署脚本（SSH 批量下发）

假设你有一份服务器清单 `hosts.txt`：

```text
root@10.0.0.11
root@10.0.0.12
root@10.0.0.13
```

部署脚本 `deploy_agents.sh`：

```bash
#!/usr/bin/env bash
set -e

IMAGE_NAME="your-registry/iperf-agent:latest"
AGENT_PORT=8000   # 控制面 HTTP 端口
IPERF_PORT=5201   # 测试端口

HOSTS_FILE="hosts.txt"

while read -r HOST; do
  [ -z "$HOST" ] && continue
  echo "==== 部署到 $HOST ===="

  ssh -o StrictHostKeyChecking=no "$HOST" "
    command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh

    docker pull $IMAGE_NAME

    docker rm -f iperf-agent || true

    docker run -d --name iperf-agent \
      --restart=always \
      -p ${AGENT_PORT}:8000 \
      -p ${IPERF_PORT}:${IPERF_PORT}/tcp \
      -p ${IPERF_PORT}:${IPERF_PORT}/udp \
      $IMAGE_NAME
  "
done < "$HOSTS_FILE"

echo "全部节点部署完成。"
```

之后 master 就能通过 `http://<节点IP>:8000` 调用各个 agent 的 API。

---

## 四、Master 端：Docker 化的 Web 管理平台

### 1. 功能划分

**master-api（FastAPI）：**

* 管理节点：

  * `POST /nodes` 注册节点（名称、IP、AGENT_PORT、备注）
  * `GET /nodes` 列表
* 管理测试任务：

  * `POST /tests` 创建测试任务

    * 指定 `src_node_id`、`dst_node_id`、参数（协议、并发、时长）
    * API 内部发 HTTP 请求到源节点的 `/run_test`，目标为目标节点 IP
    * 将结果写入数据库
  * `GET /tests` 查询历史结果

**master-web（前端）：**

* 展示节点列表（表格）
* “新建测试”表单：下拉选择源节点/目标节点、协议、参数
* “历史测试”列表 + 详情页：

  * 显示总带宽、每流带宽、时延等
  * 折线图展示 throughput 随时间变化（iperf3 JSON 里有 intervals）

### 2. master docker-compose 示例

```yaml
version: "3.8"

services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: iperf
      POSTGRES_PASSWORD: iperf_pass
      POSTGRES_DB: iperf_db
    volumes:
      - db_data:/var/lib/postgresql/data

  master-api:
    build: ./master-api
    environment:
      DATABASE_URL: postgresql://iperf:iperf_pass@db:5432/iperf_db
    depends_on:
      - db
    ports:
      - "9000:8000"   # 外部访问 API
    restart: always

  master-web:
    build: ./master-web
    depends_on:
      - master-api
    ports:
      - "9001:80"     # Web 管理界面
    restart: always

volumes:
  db_data:
```

### 3. master-api 核心逻辑示意（FastAPI）

下面是简化版逻辑（只保留关键部分，加个 sqlite 也行）：

```python
# master-api/main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
import httpx
from sqlalchemy import create_engine, Column, Integer, String, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = "sqlite:///./iperf.db"  # Demo，用 PostgreSQL 时改成 env

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Node(Base):
    __tablename__ = "nodes"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    ip = Column(String, nullable=False)
    agent_port = Column(Integer, default=8000)


class TestResult(Base):
    __tablename__ = "test_results"
    id = Column(Integer, primary_key=True, index=True)
    src_node_id = Column(Integer, ForeignKey("nodes.id"))
    dst_node_id = Column(Integer, ForeignKey("nodes.id"))
    protocol = Column(String)
    params = Column(JSON)
    raw_result = Column(JSON)

    src_node = relationship("Node", foreign_keys=[src_node_id])
    dst_node = relationship("Node", foreign_keys=[dst_node_id])


Base.metadata.create_all(bind=engine)


app = FastAPI()


class NodeCreate(BaseModel):
    name: str
    ip: str
    agent_port: int = 8000


class TestCreate(BaseModel):
    src_node_id: int
    dst_node_id: int
    protocol: str = "tcp"
    duration: int = 10
    parallel: int = 1
    port: int = 5201


@app.post("/nodes")
def create_node(node: NodeCreate):
    db = SessionLocal()
    obj = Node(name=node.name, ip=node.ip, agent_port=node.agent_port)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    db.close()
    return obj


@app.get("/nodes", response_model=List[NodeCreate])
def list_nodes():
    db = SessionLocal()
    nodes = db.query(Node).all()
    db.close()
    return nodes


@app.post("/tests")
async def create_test(test: TestCreate):
    db = SessionLocal()
    src = db.query(Node).get(test.src_node_id)
    dst = db.query(Node).get(test.dst_node_id)
    if not src or not dst:
        db.close()
        raise HTTPException(status_code=404, detail="node not found")

    agent_url = f"http://{src.ip}:{src.agent_port}/run_test"
    payload = {
        "target": dst.ip,
        "port": test.port,
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel
    }

    async with httpx.AsyncClient(timeout=test.duration + 15) as client:
        resp = await client.post(agent_url, json=payload)
    if resp.status_code != 200:
        db.close()
        raise HTTPException(status_code=500, detail=resp.text)

    data = resp.json()
    obj = TestResult(
        src_node_id=src.id,
        dst_node_id=dst.id,
        protocol=test.protocol,
        params=payload,
        raw_result=data,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    db.close()
    return {"status": "ok", "test_id": obj.id}
```

> 说明：
>
> * 这里用 sqlite 做 demo，你实际用 PostgreSQL 把 `DATABASE_URL` 换成 compose 里的那条即可。
> * `raw_result` 直接存 iperf3 原始 JSON，前端再解析绘图。

---

## 五、Web 管理界面功能建议（不赘述具体前端代码）

前端你完全可以用你熟悉的栈（React / Vue 任意）：

**页面 1：节点管理**

* 表格字段：名称、IP、Agent 端口、状态（调用 `/health` 检测）、备注
* 操作：新增 / 修改 / 删除 / Ping 测试

**页面 2：发起测试**

* 左侧选择源节点，下拉
* 右侧选择目标节点（可以多选，循环创建多条任务）
* 可选参数：

  * 协议：TCP / UDP
  * 时长：默认 10s
  * 并发流数：默认 1
* 提交后立即调用 `/tests`，返回 test_id
* 可跳转到“测试详情”

**页面 3：测试历史 & 详情**

* 列表：时间、源 → 目标、协议、带宽摘要（下行/上行）、状态
* 点击某条进入详情：

  * 用图表（如 ECharts）展示 throughput 随时间变化（从 `raw_result["intervals"]` 里读）
  * 显示：

    * 平均带宽（Mbit/s）
    * 抖动、丢包（UDP）
    * RTT（如果有）

---

## 六、你可以怎么落地实施

1. **先本地起一个 agent 容器测通：**

   * 构建 `iperf-agent` 镜像
   * 在一台测试机上 `docker run` 出来
   * `curl http://<ip>:8000/health`
   * `curl -X POST http://<ip>:8000/start_server`
   * 再手动用另一台机器 `iperf3 -c <ip> -p 5201 -t 5` 验证

2. **把 deploy_agents.sh 跑一遍，把所有节点拉起来**

3. **在 master 上把 master-api 跑起来（先不管前端），用 Postman/ curl 走一遍 API 流程**

4. **最后再做前端：**先用简单的表格 + 表单 + 一个折线图即可，后续再美化。

* 把 `iperf-agent` 和 `master-api` 拆成一个 Git 仓库的目录结构（含 `docker-compose.yml`）
* 或者根据你现有的某台测试环境（比如一台 KVM + 几个 NAT VPS）帮你把端口规划一下。
