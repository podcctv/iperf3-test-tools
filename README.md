# iperf3-test-tools / iperf3 测试工具包

A lightweight **master/agent** toolkit for orchestrating iperf3 tests across multiple servers. The master API keeps track of nodes, triggers tests, stores results, and polls agents for health and logs.

一个轻量级的 **主控/代理** 工具集，可在多台服务器间编排 iperf3 测试。主控 API 负责管理节点、触发测试、存储结果，并轮询代理获取状态与日志。

## Architecture / 架构概览

* **master-api (FastAPI + Postgres)** – central control plane, REST API, embedded dashboard, remote agent lifecycle helpers (redeploy/remove/logs). Runs via Docker Compose with a Postgres service by default.
* **agent (Flask + iperf3)** – lightweight container exposing control endpoints and the iperf3 server/client binary.
* **deploy scripts** – `install_master.sh` sets up the master stack (API + dashboard + optional local agent); `install_agent.sh` builds and runs an agent-only container on any host; `deploy_agents.sh` streams the agent image to SSH targets listed in `hosts.txt`.

## Prerequisites / 先决条件

* Docker & Docker Compose available on the target host(s).
* Outbound internet access if you want the installers to auto-download the repository or Docker images.

## Installation / 安装

### Master node (API + dashboard + optional local agent) / 主控节点

```bash
./install_master.sh [--deploy-remote] [--master-port 9000] [--web-port 9100] [--agent-port 8000] [--iperf-port 5201] [--no-start-server]
```

* Auto-updates the git checkout when possible, builds the master-api image, brings up Postgres, and starts the dashboard on `http://<host>:9100/web` (password `iperf-pass` by default).
* With `--deploy-remote`, the script will also call `./deploy_agents.sh` to push the agent image to the SSH hosts listed in `hosts.txt`.

### Agent-only host / 仅部署代理

`install_agent.sh` now works both **inside** the repository and as a standalone file downloaded elsewhere. If no `agent/` directory is found next to the script, it will fetch the repository to `~/.cache/iperf3-test-tools/agent-build` via `git` (or a GitHub tarball as fallback) before building the image.

```bash
# default ports: agent API 8000, iperf3 5201
bash ./install_agent.sh [--agent-port 8000] [--iperf-port 5201] [--no-start-server] [--repo-url <url>] [--repo-ref <ref>]
```

After installation the script prints the URL you can register on the master dashboard, e.g. `http://<agent-ip>:8000` with iperf3 port `5201`.

### Remote agent rollout via SSH / SSH 批量部署

1. Add one host per line to `hosts.txt` (format `user@ip`, optional SSH config).
2. Build the agent image locally if not present:
   ```bash
   docker build -t iperf-agent:latest ./agent
   ```
3. Run `./deploy_agents.sh` to stream the image and start containers remotely.

## Running the master stack manually / 手动启动主控服务

```bash
# Build images
docker build -t iperf-agent:latest ./agent
docker-compose build master-api

# Launch API + dashboard + Postgres
MASTER_API_PORT=9000 MASTER_WEB_PORT=9100 docker-compose up -d
```

* API: `http://localhost:9000`
* Dashboard: `http://localhost:9100/web` (password set by `DASHBOARD_PASSWORD`, default `iperf-pass`).

## API & dashboard workflow / 使用流程

1. **Register nodes / 注册节点**
   ```bash
   curl -X POST http://localhost:9000/nodes \
     -H "Content-Type: application/json" \
     -d '{"name":"node-a","ip":"10.0.0.11","agent_port":8000,"description":"rack1"}'
   ```
2. **Check live status / 查看实时状态**
   ```bash
   curl http://localhost:9000/nodes/status
   ```
   The master polls each agent's `/health` endpoint and reports `online/offline`, `server_running`, and a timestamp.
3. **Run a test / 创建并运行测试**
   ```bash
   curl -X POST http://localhost:9000/tests \
     -H "Content-Type: application/json" \
     -d '{"src_node_id":1,"dst_node_id":2,"protocol":"tcp","duration":10,"parallel":1,"port":5201}'
   ```
   The master calls the source agent's `/run_test`, stores the raw iperf3 JSON output, and returns the record.
4. **View results / 查看结果**
   ```bash
   curl http://localhost:9000/tests
   ```

All of these actions are also exposed in the web dashboard (add node, run test, view status, redeploy/remove agents, view agent logs).

## Environment variables / 环境变量

* `DATABASE_URL` – SQLAlchemy connection string (default Postgres via Compose, fallback `sqlite:///./iperf.db`).
* `REQUEST_TIMEOUT` – HTTP timeout for agent health checks and test dispatch (seconds).
* `DASHBOARD_PASSWORD` – Web dashboard password (default `iperf-pass`).
* `DASHBOARD_SECRET` – Secret used to sign the dashboard auth cookie (default `iperf-dashboard-secret`).
* `DASHBOARD_COOKIE_NAME` – Auth cookie name (default `iperf_dashboard_auth`).
* `AGENT_CONFIG_PATH` – Path where dashboard persists remote agent configs (default `./agent_configs.json`).
* `AGENT_IMAGE` – Docker image tag used when (re)deploying agents (default `iperf-agent:latest`).

## Notes / 补充说明

* Agent status is derived from live `/health` probes; unreachable nodes show as `offline`.
* `deploy_agents.sh` streams the local `iperf-agent:latest` image to remote hosts if missing—build it locally first.
* Dashboard-driven remote management (redeploy/remove container, view logs) persists inventory in `agent_configs.json` so settings survive container restarts.
