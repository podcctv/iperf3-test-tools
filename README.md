# iperf3-test-tools / iperf3 测试工具包

一个轻量级的 **主控/代理** 分布式网络测试工具，支持一键安装、自动更新、可视化监控。

A lightweight **master/agent** distributed network testing toolkit with one-click installation, auto-update, and visual monitoring.

## ✨ 核心特性 / Key Features

- 🚀 **一键安装更新** - 单条命令完成安装和更新，自动检测版本差异
- 📊 **可视化面板** - 实时监控节点状态、测试结果、流媒体解锁
- ⏰ **定时任务** - 支持周期性测试，自动生成趋势图表
- 🌐 **分布式架构** - Master/Agent 模式，轻松管理多节点
- 🔄 **远程管理** - 通过面板一键重部署、查看日志、管理容器

## 🎯 快速开始 / Quick Start

### 方式一：一键安装脚本（推荐）

**主控节点（Master + Dashboard）：**

```bash
# 下载并运行一键安装脚本
curl -fsSL https://github.com/podcctv/iperf3-test-tools/blob/main/update_iperf3_master.sh | bash

# 或者使用 wget
wget -qO- https://github.com/podcctv/iperf3-test-tools/blob/main/update_iperf3_master.sh | bash
```

安装完成后访问：`http://your-ip:9100/web`（默认密码：`iperf-pass`）

**测试节点（Agent Only）：**

```bash
# 下载并运行 Agent 安装脚本
curl -fsSL https://raw.githubusercontent.com/podcctv/iperf3-test-tools/update_iperf3_master.sh | bash

# 或者使用 wget
wget -qO- https://raw.githubusercontent.com/podcctv/iperf3-test-tools/update_iperf3_master.sh | bash
```

### 方式二：克隆仓库安装

```bash
# 克隆仓库
git clone https://github.com/podcctv/iperf3-test-tools.git
cd iperf3-test-tools

# 安装主控节点
./install_master.sh

# 或安装测试节点
./install_agent.sh
```

## 🔄 一键更新 / One-Click Update

项目提供了智能更新脚本，自动检测版本并更新：

```bash
# 在项目目录运行
bash update_iperf3_master.sh
```

**更新流程：**
1. ✅ 自动检测本地和远程版本差异
2. ✅ 清理本地修改，同步最新代码
3. ✅ 提供交互式安装选项：
   - 自动安装 Master（含本机 Agent）
   - 自动安装 Agent（仅测试节点）
   - 手动安装 Agent（NAT VPS 指定端口）
   - 仅更新代码（不执行安装）

**示例输出：**
```
[INFO] Checking iperf3-test-tools...
[INFO] Local:  125e1e8
[INFO] Remote: 4f92a5c
[INFO] New version detected. Updating...
[INFO] Update completed.

================ 安装选项 ================
1) 自动安装 master（含本机 agent 容器）
2) 自动安装 agent（仅作为测试节点）
3) 手动安装 agent（NAT VPS 指定端口）
4) 不执行安装（仅更新代码）
=========================================
```

## 📦 架构说明 / Architecture

```
┌─────────────────────────────────────┐
│  Master Node (主控节点)              │
│  ┌─────────────────────────────┐   │
│  │ FastAPI + PostgreSQL        │   │
│  │ - REST API                  │   │
│  │ - Web Dashboard             │   │
│  │ - Scheduler                 │   │
│  └─────────────────────────────┘   │
│  ┌─────────────────────────────┐   │
│  │ Local Agent (可选)           │   │
│  └─────────────────────────────┘   │
└─────────────────────────────────────┘
           │
           │ HTTP API
           ▼
┌─────────────────────────────────────┐
│  Agent Nodes (测试节点)              │
│  ┌──────────┐  ┌──────────┐        │
│  │ Agent 1  │  │ Agent 2  │  ...   │
│  │ Flask    │  │ Flask    │        │
│  │ iperf3   │  │ iperf3   │        │
│  └──────────┘  └──────────┘        │
└─────────────────────────────────────┘
```

**组件说明：**
- **Master API** - FastAPI + PostgreSQL，提供 REST API 和 Web 面板
- **Agent** - Flask + iperf3，轻量级测试节点
- **Scheduler** - APScheduler，支持定时任务和周期性测试

## 🎨 功能特性 / Features

### 1. 节点管理
- ✅ 自动发现节点状态（在线/离线）
- ✅ 实时监控 iperf3 服务器状态
- ✅ 远程启动/停止 iperf3 服务器
- ✅ 自动同步 iperf3 端口变化

### 2. 测试功能
- ✅ TCP/UDP 协议测试
- ✅ 单向/双向测试
- ✅ 并行连接测试
- ✅ 自定义带宽、数据包大小
- ✅ 测试结果可视化（图表 + 表格）

### 3. 定时任务
- ✅ 创建周期性测试任务
- ✅ 24小时趋势图（平滑线图）
- ✅ 历史记录查询（可折叠面板）
- ✅ 自动重试和错误提示
- ✅ 手动触发执行

### 4. 流媒体检测
- ✅ Netflix、Disney+、YouTube 等解锁检测
- ✅ ChatGPT、Gemini 等 AI 服务检测
- ✅ 自动缓存结果（24小时）
- ✅ 支持手动刷新

### 5. 骨干网延迟
- ✅ 三大运营商骨干网延迟监控
- ✅ 自动缓存结果（60秒）
- ✅ 实时更新显示

## 🔧 高级配置 / Advanced Configuration

### 自定义端口

**Master 节点：**
```bash
./install_master.sh \
  --master-port 9000 \
  --web-port 9100 \
  --agent-port 8000 \
  --iperf-port 62001
```

**Agent 节点：**
```bash
./install_agent.sh \
  --agent-port 8000 \
  --iperf-port 62001
```

### NAT VPS 端口映射

对于需要端口映射的 NAT VPS：

```bash
./install_agent.sh \
  --agent-port 20730 \
  --agent-listen-port 8000 \
  --iperf-port 20735
```

### 批量部署 Agent

创建 `hosts.txt` 文件：
```
root@10.0.0.11 8000 62001
root@10.0.0.12 8001 62002
root@10.0.0.13:2222 8000 62001
```

执行批量部署：
```bash
docker build -t iperf-agent:latest ./agent
./deploy_agents.sh --hosts-file hosts.txt
```

## 🔐 密码管理 / Password Management

### 默认密码
- 默认密码：`iperf-pass`
- 访问地址：`http://your-ip:9100/web`

### 修改密码
1. 登录面板后，点击右上角"修改密码"
2. 输入新密码（至少6位）并确认

### 重置密码
如果忘记密码，可以通过命令行重置：

```bash
# 在项目目录运行
docker compose exec master-api python -m app.auth --set-password 'YourNewPass' --force

# 查看密码文件位置
docker compose exec master-api python -m app.auth --show-location
```

## 📊 API 使用示例 / API Examples

### 注册节点
```bash
curl -X POST http://localhost:9000/nodes \
  -H "Content-Type: application/json" \
  -d '{
    "name": "node-tokyo",
    "ip": "10.0.0.11",
    "agent_port": 8000,
    "iperf_port": 62001,
    "description": "Tokyo VPS"
  }'
```

### 查看节点状态
```bash
curl http://localhost:9000/nodes/status
```

### 运行测试
```bash
curl -X POST http://localhost:9000/tests \
  -H "Content-Type: application/json" \
  -d '{
    "src_node_id": 1,
    "dst_node_id": 2,
    "protocol": "tcp",
    "duration": 10,
    "parallel": 1
  }'
```

### 创建定时任务
```bash
curl -X POST http://localhost:9000/schedules \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Tokyo-HK Daily Test",
    "src_node_id": 1,
    "dst_node_id": 2,
    "protocol": "tcp",
    "duration": 5,
    "interval_seconds": 3600,
    "enabled": true
  }'
```

## 🌍 环境变量 / Environment Variables

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DATABASE_URL` | PostgreSQL 连接字符串 | `postgresql://...` |
| `DASHBOARD_PASSWORD` | 面板密码 | `iperf-pass` |
| `MASTER_API_PORT` | Master API 端口 | `9000` |
| `MASTER_WEB_PORT` | Web 面板端口 | `9100` |
| `REQUEST_TIMEOUT` | Agent 请求超时（秒） | `10` |
| `AGENT_IMAGE` | Agent Docker 镜像 | `iperf-agent:latest` |
| `STATE_RECENT_TESTS` | 保留最近测试数量 | `50` |

## 🐛 故障排查 / Troubleshooting

### 1. 端口冲突
```bash
# 检查端口占用
netstat -tulpn | grep :9000

# 使用自定义端口重新安装
./install_master.sh --master-port 19000 --web-port 19100
```

### 2. 容器无法启动
```bash
# 查看容器日志
docker logs master-api-master-api-1

# 重新构建并启动
docker-compose down
docker-compose build
docker-compose up -d
```

### 3. Agent 连接失败
```bash
# 检查 Agent 状态
curl http://agent-ip:8000/health

# 检查防火墙
ufw allow 8000/tcp
ufw allow 62001/tcp
```

### 4. 定时任务失败
- 检查 `/debug/failures` 端点查看详细错误
- 确认目标节点的 iperf3 服务器正在运行
- 查看 master-api 容器日志

## 📝 更新日志 / Changelog

### v1.0.0 (Latest)
- ✅ 一键安装和更新脚本
- ✅ 定时任务功能
- ✅ 平滑线图显示
- ✅ 可折叠历史记录
- ✅ 自动端口同步
- ✅ 流媒体解锁检测
- ✅ Netflix Guest Mode 检测优化

## 📄 License

MIT License - 详见 [LICENSE](LICENSE) 文件

## 🤝 贡献 / Contributing

欢迎提交 Issue 和 Pull Request！

---

**快速链接：**
- [安装脚本更新指南](docs/script-update-guide.md)
- [API 文档](http://your-ip:9000/docs)
- [GitHub Issues](https://github.com/podcctv/iperf3-test-tools/issues)
