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

## ⚡ 一键安装

### 安装 Dashboard（监控面板）

```bash
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) dashboard
```

### 安装 Agent（每台VPS上执行）

```bash
# 交互式安装
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) agent

# 一行命令安装（无需交互）
DASHBOARD_URL=http://1.1.1.1:8080 SERVER_NAME="美国-01" TAGS="美西,Web" \
  bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) agent

# 带鉴权
DASHBOARD_URL=http://1.1.1.1:8080 SERVER_NAME="JP-01" TAGS="亚太" AUTH_TOKEN="your-secret" \
  bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) agent
```

### 卸载

```bash
bash <(curl -sL https://raw.githubusercontent.com/Misukaclaw/server-probe/main/install.sh) uninstall
```

---

## 功能

| 模块 | 数据 |
|------|------|
| 🖥 CPU | 使用率、型号、核心数、温度 |
| 💾 内存 | 使用率、已用/总量、Swap |
| 💿 磁盘 | 各挂载点容量、使用率 |
| 🌐 网络 | 流量统计、**实时速率** |
| 📊 负载 | 1/5/15分钟负载、进程数 |
| 🐧 系统 | 发行版、内核、架构、虚拟化 |
| 🏷️ 标签 | 支持按标签分组筛选 |

## 特点

- 🪶 Agent ~3MB，Dashboard ~10MB
- 🎨 暗色主题，3秒实时刷新
- 📱 响应式，手机/平板/桌面自适应
- ⚡ 零依赖，仅 Python 3 标准库
- 🚀 一键安装，兼容 Debian/Ubuntu/CentOS/Alpine
- 🔐 Token 鉴权，防恶意上报
- 📊 按标签分组，按 CPU/内存/负载排序
- 💾 数据持久化，重启不丢
- 🔄 Agent 指数退避，Dashboard 并发支持

## 配置

### Dashboard

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `PORT` | 8080 | 监听端口 |
| `OFFLINE_TIMEOUT` | 15 | 超过多少秒判定离线 |
| `AUTH_TOKEN` | 空 | 鉴权 Token，留空不鉴权 |
| `PERSIST_FILE` | 空 | 持久化文件路径，如 `/opt/server-probe/data.json` |
| `PERSIST_INTERVAL` | 60 | 持久化间隔秒 |

### Agent

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `DASHBOARD_URL` | http://localhost:8080 | Dashboard地址 |
| `SERVER_NAME` | 主机名 | 服务器显示名称 |
| `REPORT_INTERVAL` | 3 | 上报间隔（秒） |
| `AUTH_TOKEN` | 空 | 鉴权 Token，需与Dashboard一致 |
| `TAGS` | 空 | 标签，逗号分隔，如 `美西,数据库` |
| `PROBE_SKIP_SSL` | 空 | 设为1跳过SSL验证 |

## 系统要求

- Python 3.6+
- Linux（读取 /proc 文件系统）

## License

MIT
