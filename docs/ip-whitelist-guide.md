# IP 白名单系统使用指南

## 概述

IP 白名单系统防止未授权 IP 滥用 iperf3 服务。只有白名单内的 IP 可以执行测试。

## 工作原理

1. **Agent 端验证**：每个 Agent 维护一个 IP 白名单，拒绝非白名单 IP 的测试请求
2. **Master 端管理**：Master 自动生成白名单（所有节点 IP）并同步到所有 Agent
3. **自动更新**：添加/删除节点时自动同步白名单

## 配置方法

### 1. Agent 环境变量（可选）

在 `docker-compose.yml` 或启动命令中设置：

```yaml
environment:
  - ALLOWED_IPS=192.168.1.100,192.168.1.101  # 初始白名单
  - MASTER_IP=192.168.1.100                   # Master IP（用于验证更新请求）
```

### 2. Master 环境变量（可选）

```yaml
environment:
  - MASTER_IP=192.168.1.100  # Master 自己的 IP，会添加到白名单
```

## 使用方式

### 自动同步（推荐）

白名单会在以下情况自动同步：
- ✅ 添加新节点时
- ✅ 删除节点时

**无需手动操作！**

### 手动同步

如果需要手动触发同步：

```bash
curl -X POST http://master-ip:9000/admin/sync_whitelist
```

返回示例：
```json
{
  "status": "ok",
  "message": "Whitelist synced to 3/3 agents",
  "results": {
    "total_agents": 3,
    "success": 3,
    "failed": 0,
    "errors": []
  }
}
```

### 查看 Agent 白名单

```bash
curl http://agent-ip:8000/whitelist
```

返回示例：
```json
{
  "status": "ok",
  "allowed_ips": [
    "127.0.0.1",
    "192.168.1.100",
    "192.168.1.101",
    "192.168.1.102"
  ],
  "count": 4
}
```

## 测试验证

### 1. 测试白名单内 IP（应该成功）

从已注册的节点执行测试：

```bash
curl -X POST http://agent-ip:8000/run_test \
  -H "Content-Type: application/json" \
  -d '{
    "target": "target-ip",
    "port": 62001,
    "duration": 5,
    "protocol": "tcp"
  }'
```

**预期结果**：测试正常执行

### 2. 测试白名单外 IP（应该被拒绝）

从未注册的 IP 执行测试：

```bash
curl -X POST http://agent-ip:8000/run_test \
  -H "Content-Type: application/json" \
  -d '{...}'
```

**预期结果**：
```json
{
  "status": "error",
  "error": "IP not whitelisted. Contact administrator to add your IP to the whitelist."
}
```

HTTP 状态码：`403 Forbidden`

## 白名单文件位置

Agent 端白名单文件：`/app/data/ip_whitelist.txt`

可以通过 Docker volume 持久化：

```yaml
volumes:
  - ./agent_data:/app/data
```

## 日志查看

### Agent 日志

```bash
docker logs iperf-agent

# 成功的请求
# [INFO] Whitelist updated with 3 IPs from Master

# 被拒绝的请求
# [WARNING] Rejected test request from non-whitelisted IP: 1.2.3.4
```

### Master 日志

```bash
docker logs master-api-master-api-1

# 同步成功
# [INFO] Syncing whitelist with 3 IPs to 3 agents
# [INFO] Whitelist synced to node-1 (192.168.1.101)
```

## 故障排查

### 问题1：合法 IP 被拒绝

**原因**：白名单未同步或节点未注册

**解决**：
1. 确认节点已在 Master 注册
2. 手动触发同步：`POST /admin/sync_whitelist`
3. 检查 Agent 白名单：`GET /whitelist`

### 问题2：白名单同步失败

**原因**：Agent 无法访问或 MASTER_IP 配置错误

**解决**：
1. 检查 Agent 是否在线
2. 检查 MASTER_IP 环境变量
3. 查看 Master 日志中的错误信息

### 问题3：白名单为空

**原因**：首次启动，未同步

**解决**：
1. 添加至少一个节点
2. 或手动触发同步

## 安全建议

1. ✅ 设置 `MASTER_IP` 环境变量，防止非 Master 更新白名单
2. ✅ 定期检查白名单内容
3. ✅ 使用持久化存储保存白名单文件
4. ✅ 监控被拒绝的访问尝试

## 禁用白名单（不推荐）

如果需要临时禁用白名单验证，可以：

1. 设置环境变量 `ALLOWED_IPS=0.0.0.0/0`（允许所有 IP）
2. 或注释掉 Agent 代码中的 IP 验证部分

**注意**：禁用白名单会导致任何人都可以使用您的 iperf3 服务！
