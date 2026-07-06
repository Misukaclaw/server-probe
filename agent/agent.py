#!/usr/bin/env python3
"""
ServerProbe Agent - 轻量级采集端
部署在每个 VPS 上，定时向 Dashboard 上报系统数据。
"""

import json
import os
import platform
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

# ============ 配置 ============
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://localhost:8080')
REPORT_INTERVAL = int(os.environ.get('REPORT_INTERVAL', '3'))
BASIC_INFO_INTERVAL = int(os.environ.get('BASIC_INFO_INTERVAL', '300'))
SERVER_NAME = os.environ.get('SERVER_NAME', '')
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '')
TAGS = os.environ.get('TAGS', '')
# ==============================

_prev_net = {}
_net_speed = {}
_last_basic = 0
_ping_tasks = {}
_pending_ping_results = []


def read_file(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return None


def get_cpu_info():
    info = {'model': 'Unknown', 'cores': 0, 'usage': 0.0, 'temperature': None}
    cpuinfo = read_file('/proc/cpuinfo')
    if cpuinfo:
        for line in cpuinfo.split('\n'):
            if line.startswith('model name'):
                info['model'] = line.split(':', 1)[1].strip()
                break
            if line.startswith('Hardware'):
                info['model'] = line.split(':', 1)[1].strip()
                break
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
    except Exception:
        pass
    for tp in ('/sys/class/thermal/thermal_zone0/temp', '/sys/class/hwmon/hwmon0/temp1_input'):
        t = read_file(tp)
        if t:
            try:
                info['temperature'] = round(int(t) / 1000, 1)
                break
            except Exception:
                pass
    return info


def get_memory_info():
    info = {'total': 0, 'used': 0, 'free': 0, 'usage_percent': 0.0, 'swap_total': 0, 'swap_used': 0}
    meminfo = read_file('/proc/meminfo')
    if meminfo:
        d = {}
        for line in meminfo.split('\n'):
            p = line.split(':')
            if len(p) == 2:
                try:
                    d[p[0].strip()] = int(p[1].strip().split()[0])
                except Exception:
                    pass
        total = d.get('MemTotal', 0) * 1024
        available = d.get('MemAvailable', 0) * 1024
        used = total - available
        swap_total = d.get('SwapTotal', 0) * 1024
        swap_used = swap_total - d.get('SwapFree', 0) * 1024
        info = {
            'total': total,
            'used': used,
            'free': available,
            'usage_percent': round(used / total * 100, 1) if total > 0 else 0,
            'swap_total': swap_total,
            'swap_used': swap_used,
        }
    return info


def get_disk_info():
    disks = []
    try:
        df = subprocess.check_output(
            ['df', '-B1', '-x', 'tmpfs', '-x', 'devtmpfs', '-x', 'squashfs', '-x', 'overlay'],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode()
        for line in df.strip().split('\n')[1:]:
            p = line.split()
            if len(p) >= 6:
                total = int(p[1])
                used = int(p[2])
                mount = p[5]
                if total > 0 and not mount.startswith('/snap'):
                    disks.append({
                        'mount': mount,
                        'total': total,
                        'used': used,
                        'free': int(p[3]),
                        'usage_percent': round(used / total * 100, 1),
                    })
    except subprocess.TimeoutExpired:
        pass
    except Exception:
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
    global _prev_net, _net_speed
    now = time.time()
    netdev = read_file('/proc/net/dev')
    if not netdev:
        return
    cur = {}
    for line in netdev.split('\n')[2:]:
        p = line.split()
        if len(p) >= 10 and p[0].rstrip(':') != 'lo':
            cur[p[0].rstrip(':')] = {'rx': int(p[1]), 'tx': int(p[9])}
    if _prev_net:
        elapsed = now - _prev_net.get('__time__', now)
        if elapsed <= 0:
            elapsed = REPORT_INTERVAL
        _net_speed = {}
        for iface, vals in cur.items():
            if iface in _prev_net:
                _net_speed[iface] = {
                    'rx_speed': max(0, (vals['rx'] - _prev_net[iface]['rx']) / elapsed),
                    'tx_speed': max(0, (vals['tx'] - _prev_net[iface]['tx']) / elapsed),
                }
    _prev_net = {**cur, '__time__': now}


def get_load_info():
    load = {'load_1': 0, 'load_5': 0, 'load_15': 0, 'uptime': 0, 'processes': 0}
    loadavg = read_file('/proc/loadavg')
    if loadavg:
        p = loadavg.split()
        load['load_1'] = float(p[0])
        load['load_5'] = float(p[1])
        load['load_15'] = float(p[2])
        load['processes'] = int(p[3].split('/')[1])
    uptime_sec = read_file('/proc/uptime')
    if uptime_sec:
        load['uptime'] = float(uptime_sec.split()[0])
    return load


def get_os_info():
    info = {'hostname': socket.gethostname(), 'os': platform.system(), 'arch': platform.machine(), 'kernel': platform.release()}
    for path in ('/etc/os-release', '/usr/lib/os-release'):
        c = read_file(path)
        if c:
            for line in c.split('\n'):
                if line.startswith('PRETTY_NAME='):
                    info['distro'] = line.split('=', 1)[1].strip('"')
                    break
            break
    return info


def get_virtualization():
    try:
        cg = read_file('/proc/1/cgroup')
        if cg:
            if 'docker' in cg:
                return 'Docker'
            if 'kubepods' in cg:
                return 'Kubernetes'
        dmi = read_file('/sys/class/dmi/id/product_name')
        if dmi:
            for v in ('VMware', 'VirtualBox', 'KVM', 'OpenStack', 'Xen'):
                if v.lower() in dmi.lower():
                    return v
            return dmi
    except Exception:
        pass
    return 'Physical'


def collect_metrics():
    calc_network_speed()
    return {
        'cpu': get_cpu_info(),
        'memory': get_memory_info(),
        'disk': get_disk_info(),
        'network': get_network_info(),
        'network_speeds': _net_speed,
        'load': get_load_info(),
        'timestamp': int(time.time()),
    }


def collect_basic(metrics=None):
    metrics = metrics or {}
    cpu = metrics.get('cpu') or get_cpu_info()
    os_info = get_os_info()
    return {
        'name': SERVER_NAME or socket.gethostname(),
        'hostname': socket.gethostname(),
        'tags': [t.strip() for t in TAGS.split(',') if t.strip()] if TAGS else [],
        'arch': os_info.get('arch'),
        'os': os_info,
        'kernel_version': os_info.get('kernel'),
        'cpu_name': cpu.get('model'),
        'cpu_cores': cpu.get('cores'),
        'virtualization': get_virtualization(),
        'version': 'server-probe-python',
    }


def parse_tcp_target(target):
    scheme = ''
    if '://' in target:
        scheme, target = target.split('://', 1)
    target = target.split('/', 1)[0]
    default_port = 443 if scheme.lower() == 'https' else 80
    if target.startswith('[') and ']' in target:
        host = target[1:target.index(']')]
        rest = target[target.index(']') + 1:]
        port = int(rest[1:]) if rest.startswith(':') and rest[1:].isdigit() else default_port
        return host, port
    if ':' in target and target.rsplit(':', 1)[1].isdigit():
        host, port = target.rsplit(':', 1)
        return host, int(port)
    return target, default_port


def ping_icmp(task):
    target = str(task.get('target', '')).strip()
    if not target:
        return False, None, 'empty target'
    host = target.split('://')[-1].split('/')[0].split(':')[0]
    started = time.time()
    try:
        proc = subprocess.run(
            ['ping', '-c', '1', '-W', '3', host],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            text=True,
        )
        output = (proc.stdout or '') + (proc.stderr or '')
        match = re.search(r'time[=<]([0-9.]+)\s*ms', output)
        latency = float(match.group(1)) if match else (time.time() - started) * 1000
        return proc.returncode == 0, round(latency, 2), '' if proc.returncode == 0 else output.strip()[:120]
    except FileNotFoundError:
        return False, None, 'ping command not found'
    except Exception as e:
        return False, None, str(e)[:120]


def ping_tcp(task):
    target = str(task.get('target', '')).strip()
    if not target:
        return False, None, 'empty target'
    try:
        host, port = parse_tcp_target(target)
        started = time.time()
        with socket.create_connection((host, port), timeout=5):
            pass
        return True, round((time.time() - started) * 1000, 2), ''
    except Exception as e:
        return False, None, str(e)[:120]


def ping_http(task):
    target = str(task.get('target', '')).strip()
    if not target:
        return False, None, 'empty target'
    if not target.startswith(('http://', 'https://')):
        target = 'http://' + target
    started = time.time()
    try:
        req = urllib.request.Request(target, headers={'User-Agent': 'ServerProbe-Agent/1.0'}, method='GET')
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read(128)
            ok = 200 <= getattr(resp, 'status', 200) < 500
        return ok, round((time.time() - started) * 1000, 2), ''
    except urllib.error.HTTPError as e:
        return e.code < 500, round((time.time() - started) * 1000, 2), f'HTTP {e.code}'
    except Exception as e:
        return False, None, str(e)[:120]


def run_ping_task(task):
    task_type = task.get('type', 'tcp')
    if task_type == 'icmp':
        ok, latency, error = ping_icmp(task)
    elif task_type == 'http':
        ok, latency, error = ping_http(task)
    else:
        ok, latency, error = ping_tcp(task)
    return {
        'task_id': task.get('id'),
        'type': task_type,
        'target': task.get('target', ''),
        'ok': ok,
        'latency_ms': latency,
        'error': error,
        'ts': int(time.time()),
    }


def update_ping_tasks(tasks):
    global _ping_tasks
    if not isinstance(tasks, list):
        return
    next_tasks = {}
    for task in tasks:
        if not isinstance(task, dict) or not task.get('id') or not task.get('target'):
            continue
        old = _ping_tasks.get(task['id'], {})
        next_tasks[task['id']] = {
            **task,
            'next_run': old.get('next_run', 0),
        }
    _ping_tasks = next_tasks


def run_due_ping_tasks():
    now = time.time()
    for task_id, task in list(_ping_tasks.items()):
        if now < float(task.get('next_run', 0) or 0):
            continue
        task['next_run'] = now + max(10, int(task.get('interval', 60) or 60))
        _pending_ping_results.append(run_ping_task(task))
    if len(_pending_ping_results) > 100:
        del _pending_ping_results[:-100]


def collect():
    global _last_basic
    metrics = collect_metrics()
    now = time.time()
    basic_due = now - _last_basic >= BASIC_INFO_INTERVAL
    if basic_due:
        _last_basic = now
    basic = collect_basic(metrics) if basic_due else {}
    payload = {
        'name': SERVER_NAME or socket.gethostname(),
        'tags': [t.strip() for t in TAGS.split(',') if t.strip()] if TAGS else [],
        'report_interval': REPORT_INTERVAL,
        'metrics': metrics,
        **metrics,
        'os': basic.get('os') or get_os_info(),
        'virtualization': basic.get('virtualization') or get_virtualization(),
        'timestamp': int(now),
    }
    if basic:
        payload['basic'] = basic
    if _pending_ping_results:
        payload['ping_results'] = list(_pending_ping_results)
        _pending_ping_results.clear()
    return payload


def report_loop():
    url = DASHBOARD_URL.rstrip('/') + '/api/report'
    print(f'上报地址: {url}')
    print(f'间隔: {REPORT_INTERVAL}秒')

    ctx = ssl.create_default_context()
    if os.environ.get('PROBE_SKIP_SSL'):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    consecutive_failures = 0
    max_backoff = 60

    while True:
        try:
            run_due_ping_tasks()
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
                body = resp.read(65536)
            if body:
                try:
                    reply = json.loads(body.decode('utf-8'))
                    update_ping_tasks(reply.get('ping_tasks', []))
                except Exception:
                    pass

            if consecutive_failures > 0:
                print('恢复上报成功')
                consecutive_failures = 0

        except urllib.error.HTTPError as e:
            consecutive_failures += 1
            body = ''
            try:
                body = e.read().decode('utf-8', errors='replace')[:200]
            except Exception:
                pass
            print(f'HTTP {e.code} {e.reason} {body}')
        except Exception as e:
            consecutive_failures += 1
            print(f'上报失败: {e}')

        if consecutive_failures > 0:
            backoff = min(2 ** (consecutive_failures - 1), max_backoff)
            print(f'{backoff}秒后重试 (连续失败 {consecutive_failures} 次)')
            time.sleep(backoff)
        else:
            time.sleep(REPORT_INTERVAL)


if __name__ == '__main__':
    print(f'''
ServerProbe Agent 启动
   服务器名: {SERVER_NAME or socket.gethostname()}
   Dashboard: {DASHBOARD_URL}
   上报间隔: {REPORT_INTERVAL}秒
   基础信息间隔: {BASIC_INFO_INTERVAL}秒
   鉴权: {"已启用" if AUTH_TOKEN else "未启用"}
   标签: {TAGS or "无"}
''')
    report_loop()
