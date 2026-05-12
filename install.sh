#!/bin/bash
# ⚡ ServerProbe 一键安装脚本
# 用法:
#   安装 Dashboard:  bash install.sh dashboard [端口]
#   安装 Agent:      bash install.sh agent [Dashboard地址] [服务器名]
#   卸载:            bash install.sh uninstall

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

REPO="Misukaclaw/server-probe"
BRANCH="main"
INSTALL_DIR="/opt/server-probe"

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

check_root() {
    if [ "$EUID" -ne 0 ]; then
        err "请使用 root 用户运行此脚本"
        return 1
    fi
    ok "权限检查通过"
}

check_python() {
    if command -v python3 &>/dev/null; then
        PYTHON=python3
    elif command -v python &>/dev/null; then
        PYTHON=python
    else
        err "未找到 Python3，正在安装..."
        if command -v apt &>/dev/null; then
            apt update -y && apt install python3 -y
        elif command -v yum &>/dev/null; then
            yum install python3 -y
        elif command -v dnf &>/dev/null; then
            dnf install python3 -y
        else
            err "无法自动安装 Python3，请手动安装后重试"
            return 1
        fi
        PYTHON=python3
    fi
    ok "Python: $($PYTHON --version 2>&1)"
}

download() {
    local src="$1" dest="$2"
    info "下载: $src"
    if command -v curl &>/dev/null; then
        curl -sL "$src" -o "$dest"
    elif command -v wget &>/dev/null; then
        wget -q "$src" -O "$dest"
    else
        err "需要 curl 或 wget"
        return 1
    fi
    if [ ! -s "$dest" ]; then
        err "下载失败: $src"
        return 1
    fi
    ok "下载完成"
}

ask() {
    local prompt="$1" default="$2" var="$3"
    if [ -n "${!var}" ]; then
        # 环境变量已设置，直接使用
        return
    fi
    if [ -t 0 ]; then
        # 终端交互
        printf "  ${CYAN}$prompt${NC} [${default}]: "
        read -r answer
        eval "$var=\${answer:-$default}"
    else
        # 非终端（如管道），使用默认值
        eval "$var=$default"
        info "$prompt: $default (自动)"
    fi
}

install_dashboard() {
    info "安装 ServerProbe Dashboard..."
    check_root || return 1
    check_python || return 1

    # 端口配置
    local port="${2:-$PORT}"
    ask "Dashboard 监听端口" "8080" port
    info "使用端口: $port"

    mkdir -p $INSTALL_DIR/server

    # 下载
    download "https://raw.githubusercontent.com/${REPO}/${BRANCH}/server/dashboard.py" \
             "$INSTALL_DIR/server/dashboard.py" || return 1

    # 创建 systemd 服务
    cat > /etc/systemd/system/probe-dashboard.service << EOF
[Unit]
Description=ServerProbe Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/server
ExecStart=$PYTHON $INSTALL_DIR/server/dashboard.py
Environment=PORT=$port
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable probe-dashboard >/dev/null 2>&1
    systemctl restart probe-dashboard

    sleep 1
    if systemctl is-active --quiet probe-dashboard; then
        ok "Dashboard 服务已启动"
    else
        err "Dashboard 服务启动失败，查看日志: journalctl -u probe-dashboard -n 20"
        return 1
    fi

    # 获取公网IP
    local pub_ip
    pub_ip=$(curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")

    echo ""
    ok "✨ Dashboard 安装完成！"
    echo ""
    echo -e "  ${CYAN}访问地址:${NC} http://${pub_ip}:${port}"
    echo -e "  ${CYAN}管理命令:${NC}"
    echo "    systemctl status probe-dashboard    # 查看状态"
    echo "    systemctl restart probe-dashboard   # 重启"
    echo "    systemctl stop probe-dashboard      # 停止"
    echo "    journalctl -u probe-dashboard -f    # 查看日志"
    echo ""
    echo -e "  ${YELLOW}在每台 VPS 上安装 Agent:${NC}"
    echo -e "  ${GREEN}bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent http://${pub_ip}:${port} 服务器名${NC}"
}

install_agent() {
    info "安装 ServerProbe Agent..."
    check_root || return 1
    check_python || return 1

    # 配置
    local url="${2:-$DASHBOARD_URL}"
    local name="${3:-$SERVER_NAME}"
    local interval="${REPORT_INTERVAL:-3}"

    if [ -z "$url" ]; then
        ask "Dashboard 地址 (如 http://1.2.3.4:8080)" "" url
    fi
    if [ -z "$url" ]; then
        err "必须提供 Dashboard 地址"
        return 1
    fi

    if [ -z "$name" ]; then
        ask "服务器名称" "$(hostname)" name
    fi

    ask "上报间隔/秒" "3" interval

    info "Dashboard: $url"
    info "服务器名: $name"
    info "上报间隔: ${interval}s"

    mkdir -p $INSTALL_DIR/agent

    # 下载
    download "https://raw.githubusercontent.com/${REPO}/${BRANCH}/agent/agent.py" \
             "$INSTALL_DIR/agent/agent.py" || return 1

    # 创建 systemd 服务
    cat > /etc/systemd/system/probe-agent.service << EOF
[Unit]
Description=ServerProbe Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/agent
ExecStart=$PYTHON $INSTALL_DIR/agent/agent.py
Environment=DASHBOARD_URL=$url
Environment=SERVER_NAME=$name
Environment=REPORT_INTERVAL=$interval
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable probe-agent >/dev/null 2>&1
    systemctl restart probe-agent

    sleep 1
    if systemctl is-active --quiet probe-agent; then
        ok "Agent 服务已启动"
    else
        err "Agent 服务启动失败，查看日志: journalctl -u probe-agent -n 20"
        return 1
    fi

    echo ""
    ok "✨ Agent 安装完成！"
    echo ""
    echo -e "  ${CYAN}服务器名:${NC} $name"
    echo -e "  ${CYAN}Dashboard:${NC} $url"
    echo -e "  ${CYAN}管理命令:${NC}"
    echo "    systemctl status probe-agent    # 查看状态"
    echo "    systemctl restart probe-agent   # 重启"
    echo "    systemctl stop probe-agent      # 停止"
    echo "    journalctl -u probe-agent -f    # 查看日志"
    echo ""
    echo -e "  ${CYAN}修改配置:${NC} 编辑 /etc/systemd/system/probe-agent.service 后执行"
    echo "    systemctl daemon-reload && systemctl restart probe-agent"
}

uninstall() {
    check_root || return 1
    info "卸载 ServerProbe..."

    systemctl stop probe-dashboard 2>/dev/null || true
    systemctl stop probe-agent 2>/dev/null || true
    systemctl disable probe-dashboard 2>/dev/null || true
    systemctl disable probe-agent 2>/dev/null || true
    rm -f /etc/systemd/system/probe-dashboard.service
    rm -f /etc/systemd/system/probe-agent.service
    systemctl daemon-reload
    rm -rf $INSTALL_DIR

    ok "已卸载 ServerProbe"
}

# ============ 主逻辑 ============

echo ""
echo "  ⚡ ServerProbe 一键安装"
echo "  ──────────────────────"
echo ""

CMD="${1:-}"

case "$CMD" in
    dashboard|dash|d)
        install_dashboard "$@"
        ;;
    agent|a)
        install_agent "$@"
        ;;
    uninstall|remove)
        uninstall
        ;;
    *)
        echo "用法:"
        echo "  bash install.sh dashboard [端口]                          安装 Dashboard"
        echo "  bash install.sh agent [Dashboard地址] [服务器名]          安装 Agent"
        echo "  bash install.sh uninstall                                 卸载"
        echo ""
        echo "示例:"
        echo "  # 安装 Dashboard"
        echo "  bash install.sh dashboard 8080"
        echo ""
        echo "  # 安装 Agent"
        echo "  bash install.sh agent http://1.2.3.4:8080 \"美国-01\""
        echo ""
        echo "  # 用环境变量"
        echo "  DASHBOARD_URL=http://1.2.3.4:8080 SERVER_NAME=\"美国-01\" bash install.sh agent"
        echo ""
        echo "一键远程安装:"
        echo "  bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) dashboard"
        echo "  bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent http://IP:8080 名称"
        ;;
esac
