#!/bin/bash
# ⚡ ServerProbe 一键安装脚本
# 兼容: Debian/Ubuntu, CentOS/RHEL, Alpine, Arch 等
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

# 检测 init 系统
detect_init() {
    if command -v systemctl &>/dev/null && [ -d /etc/systemd/system ]; then
        INIT_SYSTEM="systemd"
    elif command -v rc-service &>/dev/null && [ -d /etc/init.d ]; then
        INIT_SYSTEM="openrc"
    elif [ -d /etc/init.d ]; then
        INIT_SYSTEM="sysvinit"
    else
        INIT_SYSTEM="unknown"
    fi
    ok "Init 系统: $INIT_SYSTEM"
}

check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "请使用 root 用户运行此脚本"
        return 1
    fi
    ok "权限检查通过"
}

check_python() {
    if command -v python3 &>/dev/null; then
        PYTHON="$(command -v python3)"
    elif command -v python &>/dev/null; then
        PYTHON="$(command -v python)"
    else
        warn "未找到 Python3，正在安装..."
        if command -v apk &>/dev/null; then
            apk add python3
        elif command -v apt &>/dev/null; then
            apt update -y && apt install python3 -y
        elif command -v yum &>/dev/null; then
            yum install python3 -y
        elif command -v dnf &>/dev/null; then
            dnf install python3 -y
        else
            err "无法自动安装 Python3，请手动安装后重试"
            return 1
        fi
        PYTHON="$(command -v python3)"
    fi
    ok "Python: $($PYTHON --version 2>&1)"
}

download() {
    local src="$1" dest="$2"
    info "下载: $src → $dest"
    if command -v curl &>/dev/null; then
        curl -sL "$src" -o "$dest"
    elif command -v wget &>/dev/null; then
        wget -q "$src" -O "$dest"
    else
        # Alpine 可能两个都没有
        if command -v apk &>/dev/null; then
            apk add curl && curl -sL "$src" -o "$dest"
        else
            err "需要 curl 或 wget"; return 1
        fi
    fi
    if [ ! -s "$dest" ]; then
        err "下载失败: $src"; return 1
    fi
    ok "下载完成"
}

ask() {
    local prompt="$1" default="$2" var="$3"
    if [ -n "${!var}" ]; then return; fi
    if [ -t 0 ]; then
        printf "  ${CYAN}$prompt${NC} [${default}]: "
        read -r answer
        eval "$var=\${answer:-$default}"
    else
        eval "$var=$default"
        info "$prompt: $default (自动)"
    fi
}

# ============ systemd 服务管理 ============

service_enable_systemd() {
    local name="$1"
    systemctl daemon-reload
    systemctl enable "$name" >/dev/null 2>&1
    systemctl restart "$name"
    sleep 1
    if systemctl is-active --quiet "$name"; then
        ok "$name 服务已启动"
    else
        err "$name 服务启动失败，查看日志: journalctl -u $name -n 20"
        return 1
    fi
}

service_stop_systemd() {
    local name="$1"
    systemctl stop "$name" 2>/dev/null || true
    systemctl disable "$name" 2>/dev/null || true
    rm -f "/etc/systemd/system/$name.service"
    systemctl daemon-reload
}

# ============ OpenRC 服务管理 (Alpine) ============

create_openrc_script() {
    local name="$1" description="$2" workdir="$3" exec_start="$4" env_vars="$5"
    local command_bin="$exec_start"
    local command_args=""
    if [[ "$exec_start" == *" "* ]]; then
        command_bin="${exec_start%% *}"
        command_args="${exec_start#* }"
    fi
    touch "/var/log/$name.log" "/var/log/$name.err"
    chmod 644 "/var/log/$name.log" "/var/log/$name.err"
    cat > "/etc/init.d/$name" << EOF
#!/sbin/openrc-run

description="$description"
command="$command_bin"
command_args="$command_args"
command_background=yes
pidfile="/run/$name.pid"
directory="$workdir"
start_stop_daemon_args="--make-pidfile"
output_log="/var/log/$name.log"
error_log="/var/log/$name.err"
$env_vars

depend() {
    need net
    after firewall
}
EOF
    chmod +x "/etc/init.d/$name"
}

service_enable_openrc() {
    local name="$1"
    rc-update add "$name" default >/dev/null 2>&1
    rc-service "$name" stop >/dev/null 2>&1 || true
    rc-service "$name" zap >/dev/null 2>&1 || true
    rc-service "$name" start
    sleep 1
    if rc-service "$name" status >/dev/null 2>&1; then
        ok "$name 服务已启动"
    else
        err "$name 服务启动失败，查看日志:"
        echo "    tail -n 50 /var/log/$name.log"
        echo "    tail -n 50 /var/log/$name.err"
        [ -s "/var/log/$name.log" ] && tail -n 20 "/var/log/$name.log"
        [ -s "/var/log/$name.err" ] && tail -n 20 "/var/log/$name.err"
        return 1
    fi
}

service_stop_openrc() {
    local name="$1"
    rc-service "$name" stop 2>/dev/null || true
    rc-update del "$name" default 2>/dev/null || true
    rm -f "/etc/init.d/$name"
}

# ============ 通用服务操作 ============

service_enable() {
    local name="$1"
    case "$INIT_SYSTEM" in
        systemd) service_enable_systemd "$name" ;;
        openrc) service_enable_openrc "$name" ;;
        *) err "不支持的 init 系统: $INIT_SYSTEM"; return 1 ;;
    esac
}

service_stop() {
    local name="$1"
    case "$INIT_SYSTEM" in
        systemd) service_stop_systemd "$name" ;;
        openrc) service_stop_openrc "$name" ;;
        *) warn "无法自动停止 $name" ;;
    esac
}

service_cmds() {
    local name="$1"
    case "$INIT_SYSTEM" in
        systemd)
            echo "    systemctl status $name      # 查看状态"
            echo "    systemctl restart $name     # 重启"
            echo "    systemctl stop $name        # 停止"
            echo "    journalctl -u $name -f      # 查看日志"
            ;;
        openrc)
            echo "    rc-service $name status     # 查看状态"
            echo "    rc-service $name restart    # 重启"
            echo "    rc-service $name stop       # 停止"
            echo "    tail -f /var/log/$name.log  # 查看日志"
            ;;
    esac
}

# ============ 安装 Dashboard ============

install_dashboard() {
    info "安装 ServerProbe Dashboard..."
    detect_init
    check_root || return 1
    check_python || return 1

    local port="${2:-$PORT}"
    local auth_token="${AUTH_TOKEN:-}"
    local persist="${PERSIST_FILE:-$INSTALL_DIR/data.json}"
    ask "Dashboard 监听端口" "8080" port
    ask "鉴权 Token (留空不鉴权)" "" auth_token
    info "使用端口: $port"

    mkdir -p $INSTALL_DIR/server

    download "https://raw.githubusercontent.com/${REPO}/${BRANCH}/server/dashboard.py" \
             "$INSTALL_DIR/server/dashboard.py" || return 1

    case "$INIT_SYSTEM" in
        systemd)
            cat > /etc/systemd/system/probe-dashboard.service << EOF
[Unit]
Description=ServerProbe Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/server
ExecStart=$PYTHON $INSTALL_DIR/server/dashboard.py
Environment=PORT=$port
Environment=AUTH_TOKEN=$auth_token
Environment=PERSIST_FILE=$persist
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
            ;;
        openrc)
            create_openrc_script \
                "probe-dashboard" \
                "ServerProbe Dashboard" \
                "$INSTALL_DIR/server" \
                "$PYTHON $INSTALL_DIR/server/dashboard.py" \
                "export PORT=$port
export AUTH_TOKEN=\"$auth_token\"
export PERSIST_FILE=\"$persist\""
            ;;
    esac

    service_enable probe-dashboard || return 1

    echo ""
    ok "✨ Dashboard 安装完成！"
    echo ""
    echo -e "  ${CYAN}访问地址:${NC} http://你的IP:${port}"
    echo -e "  ${CYAN}管理命令:${NC}"
    service_cmds probe-dashboard
    echo ""
    echo -e "  ${YELLOW}在每台 VPS 上安装 Agent:${NC}"
    echo -e "  ${GREEN}bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent http://你的IP:${port} 服务器名${NC}"
}

# ============ 安装 Agent ============

install_agent() {
    info "安装 ServerProbe Agent..."
    detect_init
    check_root || return 1
    check_python || return 1

    local url="${2:-$DASHBOARD_URL}"
    local name="${3:-$SERVER_NAME}"
    local interval="${REPORT_INTERVAL:-3}"
    local auth_token="${AUTH_TOKEN:-}"
    local tags="${TAGS:-}"

    if [ -z "$url" ]; then
        ask "Dashboard 地址 (如 http://1.1.1.1:8080)" "" url
    fi
    if [ -z "$url" ]; then
        err "必须提供 Dashboard 地址"; return 1
    fi
    if [ -z "$name" ]; then
        ask "服务器名称" "$(hostname)" name
    fi
    ask "上报间隔/秒" "3" interval
    ask "鉴权 Token (留空不鉴权)" "" auth_token
    ask "标签 (逗号分隔，如 美西,数据库)" "" tags

    info "Dashboard: $url"
    info "服务器名: $name"
    info "上报间隔: ${interval}s"

    mkdir -p $INSTALL_DIR/agent

    download "https://raw.githubusercontent.com/${REPO}/${BRANCH}/agent/agent.py" \
             "$INSTALL_DIR/agent/agent.py" || return 1

    case "$INIT_SYSTEM" in
        systemd)
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
Environment=AUTH_TOKEN=$auth_token
Environment=TAGS=$tags
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
            ;;
        openrc)
            create_openrc_script \
                "probe-agent" \
                "ServerProbe Agent" \
                "$INSTALL_DIR/agent" \
                "$PYTHON $INSTALL_DIR/agent/agent.py" \
                "export DASHBOARD_URL=\"$url\"
export SERVER_NAME=\"$name\"
export REPORT_INTERVAL=$interval
export AUTH_TOKEN=\"$auth_token\"
export TAGS=\"$tags\""
            ;;
    esac

    service_enable probe-agent || return 1

    echo ""
    ok "✨ Agent 安装完成！"
    echo ""
    echo -e "  ${CYAN}服务器名:${NC} $name"
    echo -e "  ${CYAN}Dashboard:${NC} $url"
    echo -e "  ${CYAN}管理命令:${NC}"
    service_cmds probe-agent
}

# ============ 卸载 ============

uninstall() {
    detect_init
    check_root || return 1
    info "卸载 ServerProbe..."

    service_stop probe-dashboard
    service_stop probe-agent
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
        echo "  bash install.sh dashboard 8080"
        echo "  bash install.sh agent http://1.1.1.1:8080 \"美国-01\""
        echo ""
        echo "一键远程安装:"
        echo "  bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) dashboard"
        echo "  bash <(curl -sL https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh) agent http://1.1.1.1:8080 名称"
        ;;
esac
