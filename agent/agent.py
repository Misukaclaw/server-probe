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
import threading
import sys

# ============ 配置 ============
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://localhost:8080')
REPORT_INTERVAL = int(os.environ.get('REPORT_INTERVAL', '3'))
SERVER_NAME = os.environ.get('SERVER_NAME', '')
# ==============================

def read_file(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except:
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
    except:
        pass
    for tp in ['/sys/class/thermal/thermal_zone0/temp', '/sys/class/hwmon/hwmon0/temp1_input']:
        t = read_file(tp)
        if t:
            try: info['temperature'] = round(int(t) / 1000, 1); break
            except: pass
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
        df = subprocess.check_output(['df', '-B1', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs', '-x', 'overlay'],
                                     stderr=subprocess.DEVNULL).decode()
        for line in df.strip().split('\n')[1:]:
            p = line.split()
            if len(p) >= 6:
                total = int(p[1])
                used = int(p[2])
                mount = p[5]
                if total > 0 and not mount.startswith('/snap'):
                    disks.append({'mount': mount, 'total': total, 'used': used,
                                  'free': int(p[3]), 'usage_percent': round(used / total * 100, 1)})
    except:
        pass
    return disks

def get_network_info():
    interfaces = []
    netdev = read_file('/proc/net/dev')
    if netdev:
        for line in netdev.split('\n')[2:]:
            p = line.split()
            if len(p) >= 10 and p[0].rstrip(':') != 'lo':
                interfaces.append({'interface': p[0].rstrip(':'), 'rx_bytes': int(p[1]), 'tx_bytes': int(p[9])})
    return interfaces

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
    except:
        pass
    return 'Physical'

def collect():
    """采集所有数据"""
    return {
        'name': SERVER_NAME or socket.gethostname(),
        'cpu': get_cpu_info(),
        'memory': get_memory_info(),
        'disk': get_disk_info(),
        'network': get_network_info(),
        'load': get_load_info(),
        'os': get_os_info(),
        'virtualization': get_virtualization(),
        'timestamp': int(time.time()),
    }

def report_loop():
    """定时上报循环"""
    import urllib.request
    url = DASHBOARD_URL.rstrip('/') + '/api/report'
    print(f'📡 上报地址: {url}')
    print(f'⏱  间隔: {REPORT_INTERVAL}秒')
    while True:
        try:
            data = collect()
            payload = json.dumps(data).encode('utf-8')
            req = urllib.request.Request(url, data=payload,
                                         headers={'Content-Type': 'application/json'},
                                         method='POST')
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as e:
            print(f'❌ 上报失败: {e}')
        time.sleep(REPORT_INTERVAL)

if __name__ == '__main__':
    print(f'''
⚡ ServerProbe Agent 启动
   服务器名: {SERVER_NAME or socket.gethostname()}
   Dashboard: {DASHBOARD_URL}
   上报间隔: {REPORT_INTERVAL}秒
''')
    report_loop()
