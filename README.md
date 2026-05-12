# ⚡ ServerProbe - 轻量级多服务器探针

> 多VPS集中监控 | 零依赖 | 美观暗色UI | 低资源占用

## 架构

```
┌─────────────┐     POST /api/report     ┌──────────────────┐
│  VPS-1      │ ────────────────────────→ │                  │
│  Agent      │                           │  Dashboard       │
├─────────────┤                           │  (Web UI)        │
│  VPS-2      │ ────────────────────────→ │  0.0.0.0:8080   │
│  Agent      │                           │                  │
├─────────────┤                           └──────────────────┘
│  VPS-3      │ ────────────────────────→        ↑
│  Agent      │                          浏览器访问 :8080
└─────────────┘
```

- **Dashboard** — 部署在一台服务器上，接收数据 + 展示Web面板
- **Agent** — 部署在每台VPS上，定时上报系统数据（~3MB内存）

## 功能

| 模块 | 数据 |
|------|------|
| 🖥 CPU | 使用率、型号、核心数、温度 |
| 💾 内存 | 使用率、已用/总量、Swap |
| 💿 磁盘 | 各挂载点容量、使用率 |
| 🌐 网络 | 流量统计、实时速率 |
| 📊 负载 | 1/5/15分钟负载、进程数 |
| 🐧 系统 | 发行版、内核、架构、虚拟化 |

## 快速部署

### 1. 启动 Dashboard（一台服务器）

```bash
# 直接运行
python3 server/dashboard.py

# 自定义端口
PORT=9090 python3 server/dashboard.py

# Docker
docker build -t probe-dashboard .
docker run -d --name dashboard -p 8080:8080 --restart unless-stopped probe-dashboard
```

### 2. 部署 Agent（每台VPS）

```bash
# 最简单方式
DASHBOARD_URL=http://你的DashboardIP:8080 python3 agent/agent.py

# 自定义服务器名
DASHBOARD_URL=http://你的DashboardIP:8080 SERVER_NAME="美国-01" python3 agent/agent.py

# 自定义上报间隔（秒）
DASHBOARD_URL=http://你的DashboardIP:8080 REPORT_INTERVAL=5 python3 agent/agent.py

# Docker
cd agent && docker build -t probe-agent .
docker run -d --name agent \
  -e DASHBOARD_URL=http://你的DashboardIP:8080 \
  -e SERVER_NAME="日本-01" \
  --restart unless-stopped \
  probe-agent
```

### 3. 打开面板

访问 `http://你的DashboardIP:8080` 即可看到所有服务器的状态。

## 特点

- 🪶 Agent 内存 ~3MB，Dashboard ~10MB
- 🎨 暗色主题，3秒实时刷新
- 📱 响应式，手机/平板/桌面自适应
- ⚡ 零依赖，仅 Python 3 标准库
- 🚀 单文件部署，一行命令启动
- 🔗 多服务器集中管理，在线/离线状态一目了然
- 📦 支持自定义服务器名称

## 配置

### Dashboard

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `PORT` | 8080 | 监听端口 |
| `OFFLINE_TIMEOUT` | 15 | 超过多少秒判定离线 |

### Agent

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `DASHBOARD_URL` | http://localhost:8080 | Dashboard地址 |
| `SERVER_NAME` | 主机名 | 服务器显示名称 |
| `REPORT_INTERVAL` | 3 | 上报间隔（秒） |

## 系统要求

- Python 3.6+
- Linux（读取 /proc 文件系统）

## License

MIT
