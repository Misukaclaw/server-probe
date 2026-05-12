#!/bin/bash
# ⚡ ServerProbe 一键安装脚本
# 用法:
#   安装 Dashboard:  bash install.sh dashboard
#   安装 Agent:      bash install.sh agent
#   卸载:            bash install.sh uninstall

set -e

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
err()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

check_root() {
    [ "$EUID" -ne 0 ] && err "请使用 root 用户运行此脚本"
}

check_python() {
    if command -v python3 &>/dev/null; then
        PYTHON=python3
    elif command -v python &>/dev/null; then
        PYTHON=python
    else
        err "未找到 Python3，请先安装: apt install python3 -y / yum install python3 -y"
    fi
    $PYTHON --version
}

install_dashboard() {
    info "安装 ServerProbe Dashboard..."
    check_root
    check_python

    mkdir -p $INSTALL_DIR/server

    # 下载文件
    info "下载 Dashboard 文件..."
    curl -sL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/server/dashboard.py" -o $INSTALL_DIR/server/dashboard.py
    [ ! -f $INSTALL_DIR/server/dashboard.py ] && err "下载失败，请检查网络"

    # 询问端口
    read -p "Dashboard 监听端口 [8080]: " PORT
    PORT=${PORT:-8080}

    # 创建 systemd 服务
    cat > /etc/systemd/system/probe-dashboard.service << EOF
[Unit]
Description=ServerProbe Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/server
ExecStart=$PYTHON $INSTALL_DIR/server/dashboard.py
Environment=PORT=$PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable probe-dashboard
    systemctl restart probe-dashboard

    # 获取公网IP
    PUB_IP=$(curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || echo "YOUR_IP")

    echo ""
    ok "Dashboard 安装完成！"
    echo ""
    echo -e "  ${CYAN}访问地址:${NC} http://${PUB_IP}:${PORT}"
    echo -e "  ${CYAN}管理命令:${NC}"
    echo "    systemctl status probe-dashboard   # 查看状态"
    echo "    systemctl restart probe-dashboard  # 重启"
    echo "    systemctl stop probe-dashboard     # 停止"
    echo ""
    echo -e "  ${YELLOW}Agent 安装命令 (在每台VPS上执行):${NC}"
    echo -e "  ${GREEN}bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent${NC}"
    echo -e "  或:"
    echo -e "  ${GREEN}DASHBOARD_URL=http://${PUB_IP}:${PORT} bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent${NC}"
}

install_agent() {
    info "安装 ServerProbe Agent..."
    check_root
    check_python

    # 询问 Dashboard 地址
    if [ -z "$DASHBOARD_URL" ]; then
        read -p "Dashboard 地址 (如 http://1.2.3.4:8080): " DASHBOARD_URL
    fi
    [ -z "$DASHBOARD_URL" ] && err "必须提供 Dashboard 地址"

    # 询问服务器名称
    if [ -z "$SERVER_NAME" ]; then
        read -p "服务器名称 [$(hostname)]: " SERVER_NAME
        SERVER_NAME=${SERVER_NAME:-$(hostname)}
    fi

    # 上报间隔
    if [ -z "$REPORT_INTERVAL" ]; then
        read -p "上报间隔/秒 [3]: " REPORT_INTERVAL
        REPORT_INTERVAL=${REPORT_INTERVAL:-3}
    fi

    mkdir -p $INSTALL_DIR/agent

    # 下载文件
    info "下载 Agent 文件..."
    curl -sL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/agent/agent.py" -o $INSTALL_DIR/agent/agent.py
    [ ! -f $INSTALL_DIR/agent/agent.py ] && err "下载失败，请检查网络"

    # 创建 systemd 服务
    cat > /etc/systemd/system/probe-agent.service << EOF
[Unit]
Description=ServerProbe Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/agent
ExecStart=$PYTHON $INSTALL_DIR/agent/agent.py
Environment=DASHBOARD_URL=$DASHBOARD_URL
Environment=SERVER_NAME=$SERVER_NAME
Environment=REPORT_INTERVAL=$REPORT_INTERVAL
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable probe-agent
    systemctl restart probe-agent

    echo ""
    ok "Agent 安装完成！"
    echo ""
    echo -e "  ${CYAN}服务器名:${NC} $SERVER_NAME"
    echo -e "  ${CYAN}Dashboard:${NC} $DASHBOARD_URL"
    echo -e "  ${CYAN}管理命令:${NC}"
    echo "    systemctl status probe-agent   # 查看状态"
    echo "    systemctl restart probe-agent  # 重启"
    echo "    systemctl stop probe-agent     # 停止"
    echo ""
    echo -e "  ${CYAN}修改配置:${NC} 编辑 /etc/systemd/system/probe-agent.service 后执行"
    echo "    systemctl daemon-reload && systemctl restart probe-agent"
}

uninstall() {
    check_root
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

case "${1:-}" in
    dashboard|dash|d)
        install_dashboard
        ;;
    agent|a)
        install_agent
        ;;
    uninstall|remove)
        uninstall
        ;;
    *)
        echo "用法:"
        echo "  bash install.sh dashboard          安装 Dashboard (监控面板)"
        echo "  bash install.sh agent               安装 Agent (采集端)"
        echo "  bash install.sh uninstall           卸载"
        echo ""
        echo "快捷安装:"
        echo "  DASHBOARD_URL=http://IP:PORT SERVER_NAME=名称 bash install.sh agent"
        echo ""
        echo "一键远程安装:"
        echo "  bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) dashboard"
        echo "  DASHBOARD_URL=http://IP:PORT bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent"
        ;;
esac
