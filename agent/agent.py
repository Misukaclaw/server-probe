#!/usr/bin/env python3
"""
⚡ ServerProbe Agent - 轻量级采集端
部署在每个VPS上，定时向Dashboard上报系统数据
内存占用 ~3-5MB，CPU <0.5%
"""

import json
import os
import platform
import socket
import subprocess
import time
import sys
import ssl
import urllib.request
import urllib.error

# ============ 配置 ============
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://localhost:8080')
REPORT_INTERVAL = int(os.environ.get('REPORT_INTERVAL', '3'))
SERVER_NAME = os.environ.get('SERVER_NAME', '')
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '')
TAGS = os.environ.get('TAGS', '')  # 逗号分隔，如 "美西,数据库"
# ==============================

# 网络速率追踪
_prev_net = {}
_net_speed = {}

def read_file(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        return None

def get_cpu_info():
    info = {'model': 'Unknown', 'cores': 0, 'usage': 0.0, 'temperature': None}
    cpuinfo = read_file('/proc/cpuinfo')
    if cpuinfo:
        for line in cpuinfo.split('\n'):
            if line.startswith('model name'):
                info['model'] = line.split(':', 1)[1].strip(); break
            elif line.startswith('Hardware'):
                info['model'] = line.split(':', 1)[1].strip(); break
    info['cores'] = os.cpu_count() or 0
    try:
        s1 = read_file('/proc/stat')
        time.sleep(0.1)
        s2 = read_file('/proc/stat')
        if s1 and s2:
            v1 = list(map(int, s1.split('\n')[0].split()[1:]))
            v2 = list(map(int, s2.split('\n')[0].split()[1:]))
            d_idle = v2[3] - v1[3]
            d_total = sum(v2) - sum(v1)
            if d_total > 0:
                info['usage'] = round((1 - d_idle / d_total) * 100, 1)
    except Exception as e:
        pass
    for tp in ['/sys/class/thermal/thermal_zone0/temp', '/sys/class/hwmon/hwmon0/temp1_input']:
        t = read_file(tp)
        if t:
            try: info['temperature'] = round(int(t) / 1000, 1); break
            except Exception: pass
    return info

def get_memory_info():
    info = {'total': 0, 'used': 0, 'free': 0, 'usage_percent': 0.0, 'swap_total': 0, 'swap_used': 0}
    meminfo = read_file('/proc/meminfo')
    if meminfo:
        d = {}
        for line in meminfo.split('\n'):
            p = line.split(':')
            if len(p) == 2: d[p[0].strip()] = int(p[1].strip().split()[0])
        total = d.get('MemTotal', 0) * 1024
        available = d.get('MemAvailable', 0) * 1024
        used = total - available
        swap_total = d.get('SwapTotal', 0) * 1024
        swap_used = swap_total - d.get('SwapFree', 0) * 1024
        info = {'total': total, 'used': used, 'free': available,
                'usage_percent': round(used / total * 100, 1) if total > 0 else 0,
                'swap_total': swap_total, 'swap_used': swap_used}
    return info

def get_disk_info():
    disks = []
    try:
        df = subprocess.check_output(
            ['df', '-B1', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs', '-x', 'overlay'],
            stderr=subprocess.DEVNULL, timeout=5  # 防止 NFS 等网络盘卡死
        ).decode()
        for line in df.strip().split('\n')[1:]:
            p = line.split()
            if len(p) >= 6:
                total = int(p[1])
                used = int(p[2])
                mount = p[5]
                if total > 0 and not mount.startswith('/snap'):
                    disks.append({'mount': mount, 'total': total, 'used': used,
                                  'free': int(p[3]), 'usage_percent': round(used / total * 100, 1)})
    except subprocess.TimeoutExpired:
        pass  # df 超时跳过
    except Exception as e:
        pass
    return disks

def get_network_info():
    interfaces = []
    netdev = read_file('/proc/net/dev')
    if netdev:
        for line in netdev.split('\n')[2:]:
            p = line.split()
            if len(p) >= 10 and p[0].rstrip(':') != 'lo':
                iface = p[0].rstrip(':')
                interfaces.append({'interface': iface, 'rx_bytes': int(p[1]), 'tx_bytes': int(p[9])})
    return interfaces

def calc_network_speed():
    """计算网络速率"""
    global _prev_net, _net_speed
    netdev = read_file('/proc/net/dev')
    if not netdev:
        return
    cur = {}
    for line in netdev.split('\n')[2:]:
        p = line.split()
        if len(p) >= 10 and p[0].rstrip(':') != 'lo':
            cur[p[0].rstrip(':')] = {'rx': int(p[1]), 'tx': int(p[9])}
    if _prev_net:
        _net_speed = {}
        for iface, vals in cur.items():
            if iface in _prev_net:
                _net_speed[iface] = {
                    'rx_speed': max(0, vals['rx'] - _prev_net[iface]['rx']),
                    'tx_speed': max(0, vals['tx'] - _prev_net[iface]['tx']),
                }
    _prev_net = cur

def get_load_info():
    load = {'load_1': 0, 'load_5': 0, 'load_15': 0, 'uptime': 0, 'processes': 0}
    loadavg = read_file('/proc/loadavg')
    if loadavg:
        p = loadavg.split()
        load['load_1'] = float(p[0]); load['load_5'] = float(p[1]); load['load_15'] = float(p[2])
        load['processes'] = int(p[3].split('/')[1])
    uptime_sec = read_file('/proc/uptime')
    if uptime_sec: load['uptime'] = float(uptime_sec.split()[0])
    return load

def get_os_info():
    info = {'hostname': socket.gethostname(), 'os': platform.system(),
            'arch': platform.machine(), 'kernel': platform.release()}
    for path in ['/etc/os-release', '/usr/lib/os-release']:
        c = read_file(path)
        if c:
            for line in c.split('\n'):
                if line.startswith('PRETTY_NAME='):
                    info['distro'] = line.split('=', 1)[1].strip('"'); break
            break
    return info

def get_virtualization():
    try:
        cg = read_file('/proc/1/cgroup')
        if cg:
            if 'docker' in cg: return 'Docker'
            if 'kubepods' in cg: return 'Kubernetes'
        dmi = read_file('/sys/class/dmi/id/product_name')
        if dmi:
            for v in ['VMware', 'VirtualBox', 'KVM', 'OpenStack', 'Xen']:
                if v.lower() in dmi.lower(): return v
            return dmi
    except Exception:
        pass
    return 'Physical'

def collect():
    """采集所有数据"""
    calc_network_speed()
    return {
        'name': SERVER_NAME or socket.gethostname(),
        'tags': [t.strip() for t in TAGS.split(',') if t.strip()] if TAGS else [],
        'cpu': get_cpu_info(),
        'memory': get_memory_info(),
        'disk': get_disk_info(),
        'network': get_network_info(),
        'network_speeds': _net_speed,
        'load': get_load_info(),
        'os': get_os_info(),
        'virtualization': get_virtualization(),
        'timestamp': int(time.time()),
    }

def report_loop():
    """定时上报循环，带指数退避"""
    url = DASHBOARD_URL.rstrip('/') + '/api/report'
    print(f'📡 上报地址: {url}')
    print(f'⏱  间隔: {REPORT_INTERVAL}秒')

    # SSL 上下文
    ctx = ssl.create_default_context()
    if os.environ.get('PROBE_SKIP_SSL'):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    consecutive_failures = 0
    max_backoff = 60  # 最大退避秒数

    while True:
        try:
            data = collect()
            payload = json.dumps(data).encode('utf-8')
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'ServerProbe-Agent/1.0',
            }
            if AUTH_TOKEN:
                headers['Authorization'] = f'Bearer {AUTH_TOKEN}'

            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                pass

            # 成功，重置退避
            if consecutive_failures > 0:
                print(f'✅ 恢复上报成功')
                consecutive_failures = 0

        except urllib.error.HTTPError as e:
            consecutive_failures += 1
            body = ''
            try: body = e.read().decode('utf-8', errors='replace')[:200]
            except Exception: pass
            print(f'❌ HTTP {e.code} {e.reason} {body}')
        except Exception as e:
            consecutive_failures += 1
            print(f'❌ 上报失败: {e}')

        # 指数退避: 1s, 2s, 4s, 8s, 16s, 32s, 60s, 60s...
        if consecutive_failures > 0:
            backoff = min(2 ** (consecutive_failures - 1), max_backoff)
            print(f'⏳ {backoff}秒后重试 (连续失败 {consecutive_failures} 次)')
            time.sleep(backoff)
        else:
            time.sleep(REPORT_INTERVAL)

if __name__ == '__main__':
    print(f'''
⚡ ServerProbe Agent 启动
   服务器名: {SERVER_NAME or socket.gethostname()}
   Dashboard: {DASHBOARD_URL}
   上报间隔: {REPORT_INTERVAL}秒
   鉴权: {"已启用 ✓" if AUTH_TOKEN else "未启用"}
   标签: {TAGS or "无"}
''')
    report_loop()
