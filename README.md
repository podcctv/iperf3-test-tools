# iperf3-test-tools / iperf3 测试工具包

A lightweight **master/agent** toolkit for orchestrating iperf3 tests across multiple servers. The master API keeps track of nodes, triggers tests, stores results, and polls agents for real-time health.

一个轻量级的 **主控/代理** 工具集，可在多台服务器间编排 iperf3 测试。主控 API 负责管理节点、触发测试、存储结果，并轮询代理获取实时健康状态。

## Repository layout / 仓库结构

```
agent/          # iperf3 agent container (Flask + iperf3 binary) / iperf3 代理容器
master-api/     # FastAPI control plane + persistence / FastAPI 控制面与存储
hosts.txt       # Example inventory file for deploy_agents.sh / 部署清单示例
deploy_agents.sh
```

## Quick start / 快速上手

### One-click bootstrap / 一键引导

Build images, start the master API stack, launch a local agent (with iperf3 server), and optionally deploy remote agents in one command:

使用一条命令构建镜像、启动主控 API、运行本地代理（含 iperf3 server），并可选部署远程代理：

```bash
./install_all.sh
```

You will be prompted to choose whether to install the **master**, **agent**, or **both**, and to confirm the master/agent ports before installation begins. Use flags to skip prompts in automated environments.

脚本会提示选择安装 **master**、**agent** 或 **both**，并确认端口。自动化环境可使用参数跳过交互。

Flags of interest / 常用参数：

* `--install-target <master|agent|all>` – limit installation to a specific component (defaults to interactive prompt). / 指定安装目标。
* `--no-local-agent` – skip running the local agent container. / 跳过本地代理容器。
* `--no-start-server` – do not auto-start the iperf3 server inside the local agent. / 不自动启动本地 iperf3 server。
* `--no-remote` – skip remote agent deployment. / 不部署远程代理。
* `--hosts <file>` – set an alternate inventory file for remote deployment (defaults to `hosts.txt`). / 指定主机清单。
* `--master-port <port>` – override the master API host port (default: `9000`). / 覆盖主控端口。
* `--web-port <port>` – override the dashboard host port (default: `9100`). / 覆盖网页端口。
* `--agent-port <port>` – override the agent API host port (default: `8000`). / 覆盖代理端口。

### 1) Build images / 构建镜像

```bash
docker build -t iperf-agent:latest ./agent
docker-compose build master-api
```

### 2) Deploy agents / 部署代理

Create `hosts.txt` with one SSH destination per line (e.g., `root@10.0.0.11`). Then run:

在 `hosts.txt` 中按行写入 SSH 目标（如 `root@10.0.0.11`），然后执行：

```bash
./deploy_agents.sh
```

Each agent exposes / 每个代理提供：

* Control API: `http://<agent-ip>:8000`
* iperf3 server: TCP/UDP `5201` by default (configurable per request) / 默认 TCP/UDP 端口 `5201`，可按请求调整

Key endpoints / 关键接口：

* `GET /health` – returns agent liveness and whether the iperf3 server process is running. / 返回存活与 iperf3 server 运行状态。
* `POST /start_server` – start iperf3 server (optional body `{ "port": 5201 }`). / 启动 iperf3 server。
* `POST /stop_server` – stop the iperf3 server. / 停止 iperf3 server。
* `POST /run_test` – run client test toward another node: `{ "target": "10.0.0.12", "port": 5201, "duration": 10, "protocol": "tcp", "parallel": 1 }`. / 运行指向其他节点的客户端测试。

### 3) Launch the master API / 启动主控 API

```bash
docker-compose up -d
```

The API listens on `http://localhost:9000` (port 8000 inside the container) and uses Postgres by default. To use SQLite instead, set `DATABASE_URL=sqlite:///./iperf.db` and run `uvicorn app.main:app` locally.

API 监听 `http://localhost:9000`（容器内 8000 端口），默认使用 Postgres。如需 SQLite，将 `DATABASE_URL=sqlite:///./iperf.db` 并本地运行 `uvicorn app.main:app`。

The built-in dashboard is available at `http://localhost:9100/web` by default (configurable via `--web-port`), protected by a simple password prompt. Set `DASHBOARD_PASSWORD` to override the default `iperf-pass` value before launching.

内置可视化界面默认监听 `http://localhost:9100/web`（可用 `--web-port` 修改），访问时需要输入简单密码。启动前可通过 `DASHBOARD_PASSWORD` 覆盖默认密码 `iperf-pass`。

### 4) Drive the workflow / 操作流程

1. **Register nodes / 注册节点**

   ```bash
   curl -X POST http://localhost:9000/nodes \
     -H "Content-Type: application/json" \
     -d '{"name": "node-a", "ip": "10.0.0.11", "agent_port": 8000}'
   ```

2. **Check real-time status / 查看实时状态**

   ```bash
   curl http://localhost:9000/nodes/status
   ```

   The master polls each agent's `/health` endpoint and reports `online/offline`, the agent-reported `server_running` flag, and a timestamp.

   主控轮询各代理的 `/health`，返回 `online/offline`、`server_running` 标记及时间戳。

3. **Create a test / 创建测试**

   ```bash
   curl -X POST http://localhost:9000/tests \
     -H "Content-Type: application/json" \
     -d '{"src_node_id":1, "dst_node_id":2, "protocol":"tcp", "duration":10, "parallel":1, "port":5201}'
   ```

   The master calls the source node's agent `/run_test` endpoint, stores the raw iperf3 JSON output, and returns the record.

   主控调用源节点代理的 `/run_test`，保存 iperf3 原始 JSON 输出并返回记录。

4. **List results / 查看结果**

   ```bash
   curl http://localhost:9000/tests
   ```

## Environment variables / 环境变量

* `DATABASE_URL` – SQLAlchemy connection string (defaults to SQLite `sqlite:///./iperf.db`). / SQLAlchemy 连接串，默认 SQLite。
* `REQUEST_TIMEOUT` – HTTP timeout (seconds) used for agent health checks and test dispatch (default 15s). / 健康检查与测试下发的 HTTP 超时时间（秒），默认 15。
* `DASHBOARD_PASSWORD` – Web dashboard password (default `iperf-pass`). / 网页密码（默认 `iperf-pass`）。
* `DASHBOARD_SECRET` – Secret used to sign the dashboard auth cookie (default `iperf-dashboard-secret`). / 用于签名认证 Cookie 的密钥。
* `DASHBOARD_COOKIE_NAME` – Name of the auth cookie (default `iperf_dashboard_auth`). / 认证 Cookie 名称。

## Notes / 补充说明

* Real-time agent status is derived from live `/health` probes; if an agent is unreachable, it is marked `offline`. / 在线状态基于实时 `/health` 探测，不可达即视为 offline。
* `deploy_agents.sh` now streams the local `iperf-agent:latest` image to remote hosts when it is not already present; ensure you build the agent image locally before deploying. / 如果远端缺少 `iperf-agent:latest`，`deploy_agents.sh` 会自动传输本地镜像；请先在本地构建代理镜像。
