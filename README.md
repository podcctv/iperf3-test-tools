# iperf3-test-tools

A lightweight **master/agent** toolkit for orchestrating iperf3 tests across multiple servers. The master API keeps track of nodes, triggers tests, stores results, and polls agents for real-time health.

## Repository layout

```
agent/          # iperf3 agent container (Flask + iperf3 binary)
master-api/     # FastAPI control plane + persistence
hosts.txt       # Example inventory file for deploy_agents.sh
deploy_agents.sh
```

## Quick start

### 1) Build images

```bash
docker build -t iperf-agent:latest ./agent
docker-compose build master-api
```

### 2) Deploy agents

Create `hosts.txt` with one SSH destination per line (e.g., `root@10.0.0.11`). Then run:

```bash
./deploy_agents.sh
```

Each agent exposes:

* Control API: `http://<agent-ip>:8000`
* iperf3 server: TCP/UDP `5201` by default (configurable per request)

Key endpoints:

* `GET /health` – returns agent liveness and whether the iperf3 server process is running.
* `POST /start_server` – start iperf3 server (optional body `{ "port": 5201 }`).
* `POST /stop_server` – stop the iperf3 server.
* `POST /run_test` – run client test toward another node: `{ "target": "10.0.0.12", "port": 5201, "duration": 10, "protocol": "tcp", "parallel": 1 }`.

### 3) Launch the master API

```bash
docker-compose up -d
```

The API listens on `http://localhost:9000` (port 8000 inside the container) and uses Postgres by default. To use SQLite instead, set `DATABASE_URL=sqlite:///./iperf.db` and run `uvicorn app.main:app` locally.

### 4) Drive the workflow

1. **Register nodes**

   ```bash
   curl -X POST http://localhost:9000/nodes \
     -H "Content-Type: application/json" \
     -d '{"name": "node-a", "ip": "10.0.0.11", "agent_port": 8000}'
   ```

2. **Check real-time status**

   ```bash
   curl http://localhost:9000/nodes/status
   ```

   The master polls each agent's `/health` endpoint and reports `online/offline`, the agent-reported `server_running` flag, and a timestamp.

3. **Create a test**

   ```bash
   curl -X POST http://localhost:9000/tests \
     -H "Content-Type: application/json" \
     -d '{"src_node_id":1, "dst_node_id":2, "protocol":"tcp", "duration":10, "parallel":1, "port":5201}'
   ```

   The master calls the source node's agent `/run_test` endpoint, stores the raw iperf3 JSON output, and returns the record.

4. **List results**

   ```bash
   curl http://localhost:9000/tests
   ```

## Environment variables

* `DATABASE_URL` – SQLAlchemy connection string (defaults to SQLite `sqlite:///./iperf.db`).
* `REQUEST_TIMEOUT` – HTTP timeout (seconds) used for agent health checks and test dispatch (default 15s).

## Notes

* Real-time agent status is derived from live `/health` probes; if an agent is unreachable, it is marked `offline`.
* `deploy_agents.sh` assumes the `iperf-agent:latest` image is published or locally available on the target hosts.
