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
| 🌐 网络 | 流量统计、**实时速率** |
| 📊 负载 | 1/5/15分钟负载、进程数 |
| 🐧 系统 | 发行版、内核、架构、虚拟化 |

## 特点

- 🪶 Agent ~3MB / Dashboard ~10MB
- 🎨 暗色主题，3秒实时刷新
- 📱 响应式，手机/平板/桌面自适应
- ⚡ 零依赖，仅 Python 3 标准库
- 🚀 单文件部署，一行命令启动
- 🔗 多服务器集中管理，在线/离线状态
- 🏷️ 标签分组 + 排序（按CPU/内存/负载/在线状态）
- 🔐 Token 鉴权，防止数据污染
- 💾 可选数据持久化，重启不丢
- 🐧 兼容 systemd / OpenRC (Alpine)

## ⚡ 一键安装

### 安装 Dashboard（监控面板）

```bash
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) dashboard
```

### 安装 Agent（每台VPS上执行）

```bash
# 交互式安装
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) agent

# 一行命令安装
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) agent http://1.1.1.1:8080 "美国-01"
```

### 卸载

```bash
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) uninstall
```

---

## 手动部署

### 1. 启动 Dashboard

```bash
python3 server/dashboard.py

# 自定义配置
PORT=9090 AUTH_TOKEN=your_secret PERSIST_FILE=/opt/probe/data.json python3 server/dashboard.py
```

### 2. 部署 Agent

```bash
DASHBOARD_URL=http://1.1.1.1:8080 \
SERVER_NAME="美国-01" \
TAGS="美西,数据库" \
AUTH_TOKEN=your_secret \
python3 agent/agent.py
```

### 3. 打开面板

访问 `http://1.1.1.1:8080` 即可看到所有服务器状态。

## 配置

### Dashboard

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `PORT` | 8080 | 监听端口 |
| `OFFLINE_TIMEOUT` | 15 | 超过多少秒判定离线 |
| `AUTH_TOKEN` | 空 | 鉴权Token，留空不鉴权 |
| `PERSIST_FILE` | 空 | 持久化文件路径，留空不持久化 |
| `PERSIST_INTERVAL` | 60 | 持久化间隔（秒） |

### Agent

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `DASHBOARD_URL` | http://localhost:8080 | Dashboard地址 |
| `SERVER_NAME` | 主机名 | 服务器显示名称 |
| `REPORT_INTERVAL` | 3 | 上报间隔（秒） |
| `AUTH_TOKEN` | 空 | 鉴权Token，需与Dashboard一致 |
| `TAGS` | 空 | 标签，逗号分隔（如 `美西,数据库`） |
| `PROBE_SKIP_SSL` | 空 | 设为1跳过SSL验证 |

## 安全

- **鉴权**: 设置 `AUTH_TOKEN` 后，Agent 必须携带相同Token才能上报
- **防OOM**: 限制请求体最大10KB
- **XSS防护**: 前端所有数据经HTML转义
- **Body校验**: 空/超长/非法JSON均被拒绝

## 系统要求

- Python 3.6+
- Linux（读取 /proc 文件系统）

## License

MIT
