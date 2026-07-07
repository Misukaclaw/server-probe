#!/usr/bin/env python3
"""
ServerProbe Dashboard - 集中监控面板
接收多个 VPS Agent 上报的数据，在统一页面展示。
"""

import http.server
import json
import os
import queue
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from urllib.parse import parse_qs, urlparse

# ============ 配置 ============
PORT = int(os.environ.get('PORT', '8080'))
OFFLINE_TIMEOUT = int(os.environ.get('OFFLINE_TIMEOUT', '15'))
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '')
PERSIST_FILE = os.environ.get('PERSIST_FILE', '')
PERSIST_INTERVAL = int(os.environ.get('PERSIST_INTERVAL', '60'))
MAX_BODY_SIZE = int(os.environ.get('MAX_BODY_SIZE', '65536'))
GHOST_TIMEOUT_DAYS = int(os.environ.get('GHOST_TIMEOUT_DAYS', '30'))
STABILITY_SAMPLE_INTERVAL = int(os.environ.get('STABILITY_SAMPLE_INTERVAL', '60'))
STABILITY_RETENTION_DAYS = int(os.environ.get('STABILITY_RETENTION_DAYS', '90'))
STABILITY_DISPLAY_DAYS = int(os.environ.get('STABILITY_DISPLAY_DAYS', '7'))
BASIC_INFO_INTERVAL = int(os.environ.get('BASIC_INFO_INTERVAL', '300'))
PING_HISTORY_DAYS = int(os.environ.get('PING_HISTORY_DAYS', '7'))
METRICS_HISTORY_HOURS = int(os.environ.get('METRICS_HISTORY_HOURS', '24'))
METRICS_MIN_INTERVAL = int(os.environ.get('METRICS_MIN_INTERVAL', '60'))
ALERT_CPU = float(os.environ.get('ALERT_CPU', '90'))
ALERT_MEM = float(os.environ.get('ALERT_MEM', '90'))
ALERT_DISK = float(os.environ.get('ALERT_DISK', '90'))
ALERT_SUSTAINED_SECONDS = int(os.environ.get('ALERT_SUSTAINED_SECONDS', '300'))
ALERT_COOLDOWN = int(os.environ.get('ALERT_COOLDOWN', '300'))
NOTIFY_WEBHOOK_URL = os.environ.get('NOTIFY_WEBHOOK_URL', '')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
# ==============================

servers = {}
stability = {}
server_meta = {}
ping_tasks = []
ping_history = {}
metrics_history = {}
alert_state = {}
server_status_state = {}
notify_queue = queue.Queue()
lock = threading.Lock()


def hour_bucket(ts=None):
    ts = time.time() if ts is None else ts
    return int(ts // 3600 * 3600)


def day_bucket(ts=None):
    ts = time.time() if ts is None else ts
    return int(ts // 86400 * 86400)


def normalize_bucket(bucket):
    if not isinstance(bucket, dict):
        return {'ok': 0, 'total': 0}
    ok = int(bucket.get('ok', 0) or 0)
    total = int(bucket.get('total', 0) or 0)
    return {'ok': max(0, ok), 'total': max(0, total)}


def normalize_meta(meta=None):
    meta = meta if isinstance(meta, dict) else {}
    hidden = meta.get('hidden', False)
    if isinstance(hidden, str):
        hidden = hidden.lower() in ('1', 'true', 'yes', 'on', 'hidden')
    return {
        'display_name': str(meta.get('display_name', '') or '')[:80],
        'note': str(meta.get('note', '') or '')[:180],
        'group': str(meta.get('group', '') or '')[:50],
        'hidden': bool(hidden),
        'weight': int(meta.get('weight', 0) or 0),
    }


def main_disk(disks):
    if not isinstance(disks, list) or not disks:
        return {'usage_percent': 0, 'used': 0, 'total': 0, 'mount': '/'}
    for disk in disks:
        if isinstance(disk, dict) and disk.get('mount') == '/':
            return disk
    return disks[0] if isinstance(disks[0], dict) else {'usage_percent': 0, 'used': 0, 'total': 0, 'mount': '/'}


def normalize_report(data, previous=None):
    previous = previous if isinstance(previous, dict) else {}
    data = data if isinstance(data, dict) else {}
    basic = data.get('basic') if isinstance(data.get('basic'), dict) else {}
    metrics = data.get('metrics') if isinstance(data.get('metrics'), dict) else {}
    merged = dict(previous)

    for key in ('name', 'tags', 'cpu', 'memory', 'disk', 'network', 'network_speeds', 'load', 'os', 'virtualization', 'timestamp', 'report_interval'):
        if key in data:
            merged[key] = data[key]

    if basic:
        name = basic.get('name') or basic.get('hostname')
        if name and not merged.get('name'):
            merged['name'] = name
        if 'tags' in basic and not merged.get('tags'):
            merged['tags'] = basic.get('tags') or []
        if 'os' in basic and isinstance(basic['os'], dict):
            merged['os'] = basic['os']
        else:
            os_info = dict(merged.get('os') or {})
            for src, dest in (('hostname', 'hostname'), ('arch', 'arch'), ('kernel', 'kernel'), ('kernel_version', 'kernel'), ('distro', 'distro')):
                if basic.get(src):
                    os_info[dest] = basic[src]
            if os_info:
                merged['os'] = os_info
        if basic.get('virtualization'):
            merged['virtualization'] = basic['virtualization']
        if basic.get('cpu_name') or basic.get('cpu_cores'):
            cpu = dict(merged.get('cpu') or {})
            if basic.get('cpu_name'):
                cpu['model'] = basic['cpu_name']
            if basic.get('cpu_cores') is not None:
                cpu['cores'] = basic.get('cpu_cores')
            merged['cpu'] = cpu

    if metrics:
        for key in ('cpu', 'memory', 'disk', 'network', 'network_speeds', 'load', 'timestamp'):
            if key in metrics:
                merged[key] = metrics[key]

    merged.setdefault('name', data.get('name') or basic.get('name') or basic.get('hostname') or 'unknown')
    merged.setdefault('tags', [])
    merged.setdefault('cpu', {})
    merged.setdefault('memory', {})
    merged.setdefault('disk', [])
    merged.setdefault('network', [])
    merged.setdefault('network_speeds', {})
    merged.setdefault('load', {})
    merged.setdefault('os', {})
    merged.setdefault('virtualization', '')
    merged['timestamp'] = int(merged.get('timestamp') or time.time())
    return merged


def record_metrics_sample(name, data, now=None):
    """按 METRICS_MIN_INTERVAL 节流，记录一台服务器的时序指标（须持锁调用）。"""
    now = time.time() if now is None else now
    rows = metrics_history.setdefault(name, [])
    if rows and now - rows[-1].get('ts', 0) < METRICS_MIN_INTERVAL:
        return
    disk = main_disk(data.get('disk'))
    rx_speed = tx_speed = 0.0
    speeds = data.get('network_speeds')
    if isinstance(speeds, dict):
        for s in speeds.values():
            if isinstance(s, dict):
                try:
                    rx_speed += float(s.get('rx_speed') or 0)
                    tx_speed += float(s.get('tx_speed') or 0)
                except (TypeError, ValueError):
                    pass

    def num(v):
        try:
            return round(float(v or 0), 1)
        except (TypeError, ValueError):
            return 0.0

    rows.append({
        'ts': int(now),
        'cpu': num((data.get('cpu') or {}).get('usage')),
        'mem': num((data.get('memory') or {}).get('usage_percent')),
        'disk': num(disk.get('usage_percent') if isinstance(disk, dict) else 0),
        'rx': int(rx_speed),
        'tx': int(tx_speed),
    })
    cutoff = now - METRICS_HISTORY_HOURS * 3600
    while rows and rows[0].get('ts', 0) < cutoff:
        rows.pop(0)


def prune_stability(now=None):
    cutoff = hour_bucket(now) - STABILITY_RETENTION_DAYS * 86400
    empty_names = []
    for name, buckets in stability.items():
        old_keys = []
        for k in buckets:
            try:
                if int(k) < cutoff:
                    old_keys.append(k)
            except Exception:
                old_keys.append(k)
        for k in old_keys:
            del buckets[k]
        if not buckets and name not in servers:
            empty_names.append(name)
    for name in empty_names:
        del stability[name]


def record_stability_sample(now=None):
    now = time.time() if now is None else now
    bucket = str(hour_bucket(now))
    with lock:
        for name, srv in servers.items():
            last_seen = srv.get('last_seen', 0)
            if last_seen <= 0:
                continue
            online = now - last_seen < OFFLINE_TIMEOUT
            item = stability.setdefault(name, {}).setdefault(bucket, {'ok': 0, 'total': 0})
            item['total'] = int(item.get('total', 0)) + 1
            if online:
                item['ok'] = int(item.get('ok', 0)) + 1
        prune_stability(now)


def stability_payload(name, now=None):
    now = time.time() if now is None else now
    end_hour = hour_bucket(now)
    display_days = max(1, min(STABILITY_DISPLAY_DAYS, STABILITY_RETENTION_DAYS))
    display_hours = display_days * 24
    start_hour = end_hour - (display_hours - 1) * 3600
    buckets = stability.get(name, {})
    hours = []
    ok_sum = total_sum = 0
    for ts in range(start_hour, end_hour + 1, 3600):
        b = normalize_bucket(buckets.get(str(ts), {}))
        ok = b['ok']
        total = b['total']
        ratio = round(ok / total * 100, 2) if total else None
        if total:
            ok_sum += ok
            total_sum += total
        hours.append({'ts': ts, 'ok': ok, 'total': total, 'ratio': ratio})

    end_day = day_bucket(now)
    start_day = end_day - (STABILITY_RETENTION_DAYS - 1) * 86400
    days = []
    day_ok_sum = day_total_sum = 0
    for day_ts in range(start_day, end_day + 1, 86400):
        day_ok = day_total = 0
        for hour_ts in range(day_ts, day_ts + 86400, 3600):
            b = normalize_bucket(buckets.get(str(hour_ts), {}))
            day_ok += b['ok']
            day_total += b['total']
        ratio = round(day_ok / day_total * 100, 2) if day_total else None
        if day_total:
            day_ok_sum += day_ok
            day_total_sum += day_total
        days.append({'ts': day_ts, 'ok': day_ok, 'total': day_total, 'ratio': ratio})

    return {
        'percent': round(ok_sum / total_sum * 100, 2) if total_sum else None,
        'days_percent': round(day_ok_sum / day_total_sum * 100, 2) if day_total_sum else None,
        'hours': hours,
        'days': days,
        'display_days': display_days,
        'retention_days': STABILITY_RETENTION_DAYS,
        'sample_interval': STABILITY_SAMPLE_INTERVAL,
    }


def prune_ping_history(now=None):
    now = time.time() if now is None else now
    cutoff = now - PING_HISTORY_DAYS * 86400
    empty = []
    for task_id, rows in ping_history.items():
        ping_history[task_id] = [r for r in rows if float(r.get('ts', 0) or 0) >= cutoff]
        if not ping_history[task_id] and not any(t.get('id') == task_id for t in ping_tasks):
            empty.append(task_id)
    for task_id in empty:
        del ping_history[task_id]


def ping_summary_for_server(name):
    latest = []
    for task in ping_tasks:
        task_id = task.get('id')
        if not task_id:
            continue
        rows = [r for r in ping_history.get(task_id, []) if r.get('server') == name]
        if rows:
            row = rows[-1].copy()
            row['task_name'] = task.get('name') or task.get('target')
            latest.append(row)
    latest.sort(key=lambda r: r.get('ts', 0), reverse=True)
    return {'last': latest[:4]}


def tasks_for_agent(name):
    result = []
    for task in ping_tasks:
        if not task.get('enabled', True):
            continue
        task_server = str(task.get('server', '') or '')
        if task_server and task_server != name:
            continue
        result.append({
            'id': task.get('id'),
            'name': task.get('name') or task.get('target'),
            'type': task.get('type', 'tcp'),
            'target': task.get('target', ''),
            'interval': int(task.get('interval', 60) or 60),
        })
    return result


def normalize_ping_task(payload):
    payload = payload if isinstance(payload, dict) else {}
    task_id = str(payload.get('id') or '').strip()
    existing = None
    if task_id:
        existing = next((t for t in ping_tasks if t.get('id') == task_id), None)
    base = dict(existing or {})
    task_type = str(payload.get('type', base.get('type', 'tcp')) or 'tcp').lower()
    if task_type not in ('icmp', 'tcp', 'http'):
        task_type = 'tcp'
    target = str(payload.get('target', base.get('target', '')) or '').strip()
    if not target:
        raise ValueError('target is required')
    interval = int(payload.get('interval', base.get('interval', 60)) or 60)
    interval = max(10, min(interval, 86400))
    enabled = payload.get('enabled', base.get('enabled', True))
    if isinstance(enabled, str):
        enabled = enabled.lower() not in ('0', 'false', 'no', 'off')
    return {
        'id': task_id or uuid.uuid4().hex[:12],
        'name': str(payload.get('name', base.get('name', target)) or target)[:80],
        'type': task_type,
        'target': target[:200],
        'server': str(payload.get('server', base.get('server', '')) or '')[:80],
        'interval': interval,
        'enabled': bool(enabled),
        'updated_at': int(time.time()),
    }


def ingest_ping_results(name, results, now=None):
    now = time.time() if now is None else now
    if not isinstance(results, list):
        return
    known_task_ids = {task.get('id') for task in ping_tasks}
    for result in results[:50]:
        if not isinstance(result, dict):
            continue
        task_id = str(result.get('task_id') or result.get('id') or '').strip()
        if not task_id:
            continue
        row = {
            'task_id': task_id,
            'server': name,
            'ts': float(result.get('ts') or now),
            'ok': bool(result.get('ok')),
            'latency_ms': result.get('latency_ms'),
            'error': str(result.get('error', '') or '')[:160],
            'target': str(result.get('target', '') or '')[:200],
            'type': str(result.get('type', '') or '')[:20],
        }
        ping_history.setdefault(task_id, []).append(row)
        if task_id not in known_task_ids:
            ping_history[task_id] = ping_history[task_id][-200:]
    prune_ping_history(now)


def queue_notify(title, message, level='info', server=''):
    if not (NOTIFY_WEBHOOK_URL or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)):
        return
    notify_queue.put({
        'title': title,
        'message': message,
        'level': level,
        'server': server,
        'ts': int(time.time()),
    })


def send_webhook(event):
    if not NOTIFY_WEBHOOK_URL:
        return
    payload = json.dumps(event, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        NOTIFY_WEBHOOK_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        resp.read(128)


def send_telegram(event):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    text = f"{event['title']}\n{event['message']}"
    payload = urllib.parse.urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': text}).encode('utf-8')
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        resp.read(128)


def notify_loop():
    while True:
        event = notify_queue.get()
        try:
            send_webhook(event)
        except Exception as e:
            print(f'通知 Webhook 失败: {e}')
        try:
            send_telegram(event)
        except Exception as e:
            print(f'通知 Telegram 失败: {e}')


def display_name_for(name):
    meta = server_meta.get(name, {})
    return meta.get('display_name') or name


def evaluate_resource_alerts(name, data, now=None):
    now = time.time() if now is None else now
    checks = [
        ('cpu', float((data.get('cpu') or {}).get('usage') or 0), ALERT_CPU, 'CPU'),
        ('mem', float((data.get('memory') or {}).get('usage_percent') or 0), ALERT_MEM, '内存'),
        ('disk', float(main_disk(data.get('disk')).get('usage_percent') or 0), ALERT_DISK, '磁盘'),
    ]
    for key, value, threshold, label in checks:
        state_key = f'{name}:{key}'
        state = alert_state.setdefault(state_key, {'active': False, 'since': None, 'last_sent': 0})
        over = value >= threshold
        if over:
            if state.get('since') is None:
                state['since'] = now
            sustained = now - float(state.get('since') or now)
            can_send = now - float(state.get('last_sent') or 0) >= ALERT_COOLDOWN
            if sustained >= ALERT_SUSTAINED_SECONDS and (not state.get('active') or can_send):
                state['active'] = True
                state['last_sent'] = now
                queue_notify(
                    f'{display_name_for(name)} {label} 告警',
                    f'{label} 当前 {value:.1f}%，阈值 {threshold:.0f}%，已持续 {int(sustained)} 秒。',
                    'warning',
                    name,
                )
        else:
            if state.get('active'):
                queue_notify(
                    f'{display_name_for(name)} {label} 恢复',
                    f'{label} 已恢复到 {value:.1f}%，低于阈值 {threshold:.0f}%。',
                    'info',
                    name,
                )
            state['active'] = False
            state['since'] = None


def status_loop():
    while True:
        now = time.time()
        with lock:
            for name, srv in servers.items():
                last_seen = srv.get('last_seen', 0)
                if last_seen <= 0:
                    continue
                online = now - last_seen < OFFLINE_TIMEOUT
                previous = server_status_state.get(name)
                if previous is None:
                    server_status_state[name] = online
                elif previous and not online:
                    server_status_state[name] = False
                    queue_notify(
                        f'{display_name_for(name)} 离线',
                        f'超过 {OFFLINE_TIMEOUT} 秒未收到上报。',
                        'error',
                        name,
                    )
        time.sleep(max(5, min(30, OFFLINE_TIMEOUT // 2 or 5)))


def load_persist():
    if not PERSIST_FILE:
        return
    try:
        with open(PERSIST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('version') == 3:
            server_data = data.get('servers', {})
            stability_data = data.get('stability', {})
            meta_data = data.get('server_meta', {})
            task_data = data.get('ping_tasks', [])
            history_data = data.get('ping_history', {})
            metrics_data = data.get('metrics_history', {})
        elif isinstance(data, dict) and data.get('version') == 2 and isinstance(data.get('servers'), dict):
            server_data = data.get('servers', {})
            stability_data = data.get('stability', {})
            meta_data = {}
            task_data = []
            history_data = {}
            metrics_data = {}
        else:
            server_data = data if isinstance(data, dict) else {}
            stability_data = {}
            meta_data = {}
            task_data = []
            history_data = {}
            metrics_data = {}
        with lock:
            for name, srv in server_data.items():
                raw_data = srv.get('data', {}) if isinstance(srv, dict) and 'data' in srv else srv
                servers[name] = {'data': normalize_report(raw_data), 'last_seen': 0}
            for name, buckets in stability_data.items():
                if isinstance(buckets, dict):
                    stability[name] = {str(k): normalize_bucket(v) for k, v in buckets.items()}
            for name, meta in meta_data.items():
                server_meta[name] = normalize_meta(meta)
            if isinstance(task_data, list):
                for task in task_data:
                    try:
                        ping_tasks.append(normalize_ping_task(task))
                    except Exception:
                        pass
            if isinstance(history_data, dict):
                for task_id, rows in history_data.items():
                    if isinstance(rows, list):
                        ping_history[task_id] = rows[-5000:]
            if isinstance(metrics_data, dict):
                cutoff = time.time() - METRICS_HISTORY_HOURS * 3600
                for name, rows in metrics_data.items():
                    if isinstance(rows, list):
                        kept = [r for r in rows if isinstance(r, dict) and r.get('ts', 0) >= cutoff]
                        if kept:
                            metrics_history[name] = kept[-3000:]
            prune_stability()
            prune_ping_history()
        print(f'已加载持久化数据: {len(servers)} 台服务器，{len(stability)} 份稳定性记录，{len(ping_tasks)} 个探测任务')
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'加载持久化数据失败: {e}')


def persist_loop():
    if not PERSIST_FILE:
        return
    while True:
        time.sleep(PERSIST_INTERVAL)
        try:
            with lock:
                prune_ping_history()
                data = {
                    'version': 3,
                    'servers': dict(servers),
                    'stability': {name: {k: dict(v) for k, v in buckets.items()} for name, buckets in stability.items()},
                    'server_meta': dict(server_meta),
                    'ping_tasks': list(ping_tasks),
                    'ping_history': {task_id: list(rows) for task_id, rows in ping_history.items()},
                    'metrics_history': {name: list(rows) for name, rows in metrics_history.items()},
                }
                serialized = json.dumps(data, ensure_ascii=False)
            tmp = PERSIST_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(serialized)
            os.replace(tmp, PERSIST_FILE)
        except Exception as e:
            print(f'持久化失败: {e}')


def cleanup_ghosts():
    if GHOST_TIMEOUT_DAYS <= 0:
        return
    threshold = GHOST_TIMEOUT_DAYS * 86400
    while True:
        now = time.time()
        with lock:
            ghosts = [k for k, v in servers.items() if v['last_seen'] > 0 and now - v['last_seen'] > threshold]
            for k in ghosts:
                del servers[k]
                stability.pop(k, None)
                server_status_state.pop(k, None)
                metrics_history.pop(k, None)
            prune_stability(now)
        if ghosts:
            print(f'清理 {len(ghosts)} 个幽灵条目: {ghosts}')
        time.sleep(3600)


def stability_loop():
    if STABILITY_SAMPLE_INTERVAL <= 0:
        return
    while True:
        record_stability_sample()
        time.sleep(STABILITY_SAMPLE_INTERVAL)


HTML_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小鸡儿 · VPS 状态墙</title>
<meta name="probe-auth-required" content="__AUTH_REQUIRED__">
<style>
*{box-sizing:border-box}
:root{color-scheme:light;--bg:#F4F6F4;--panel:#FFFFFF;--panel-2:#EDF1ED;--text:#15181A;--muted:#6E767C;--line:#E0E5E0;--ok:#4E8F1E;--warn:#C07A16;--bad:#DF4547;--blue:#2F6FED;--shadow:0 10px 28px rgba(25,32,28,.07);--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
[data-theme="dark"]{color-scheme:dark;--bg:#0F1211;--panel:#181C1A;--panel-2:#121614;--text:#EFF3F0;--muted:#94A09A;--line:#293029;--ok:#7BC340;--warn:#E8A63C;--bad:#EF6265;--blue:#6C9BFF;--shadow:0 14px 34px rgba(0,0,0,.4)}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.45}
button,input,select,textarea{font:inherit;color:inherit}button{cursor:pointer}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.app{width:min(1280px,calc(100vw - 28px));margin:0 auto;padding:18px 0 30px}
.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:10px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.mark{width:34px;height:34px;border-radius:9px;background:var(--blue);color:#fff;font:700 13px/34px var(--mono);text-align:center;flex:none}
.brand h1{margin:0;font-size:17px;font-weight:800}
.brand .sub{font-size:12px;color:var(--muted)}
.actions-top{display:flex;align-items:center;gap:7px}
.clock{font:700 13px var(--mono);color:var(--muted)}
.btn{border:1px solid var(--line);background:var(--panel);height:34px;border-radius:9px;padding:0 12px;font-weight:700;font-size:13px}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--text);color:var(--bg);border-color:var(--text)}
.hero{display:flex;gap:18px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow);margin-bottom:14px}
.ring{flex:none}
.rt{fill:none;stroke:var(--panel-2);stroke-width:11}
.ra{fill:none;stroke:var(--ok);stroke-width:11;stroke-linecap:round}
.rv{font:700 27px var(--mono);fill:var(--text);text-anchor:middle}
.rl{font:600 11px var(--sans);fill:var(--muted);text-anchor:middle}
.tiles{flex:1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;min-width:0}
.tile{background:var(--panel-2);border-radius:10px;padding:10px 12px;min-width:0}
.tile b{display:block;font:700 20px/1.25 var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile span{font-size:12px;color:var(--muted)}
.tile.wide{grid-column:span 2}
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.filters{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--panel);height:31px;border-radius:9px;padding:0 11px;font-size:13px;font-weight:700;color:var(--muted)}
.chip.active{background:var(--text);color:var(--bg);border-color:var(--text)}
.select{height:34px;border:1px solid var(--line);border-radius:9px;background:var(--panel);padding:0 28px 0 10px;font-weight:700;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:var(--shadow);position:relative;min-width:0;cursor:pointer;transition:border-color .15s}
.card:hover{border-color:var(--muted)}
.card.offline{border-color:color-mix(in srgb,var(--bad) 45%,var(--line))}
.acts{position:absolute;right:9px;top:9px;display:flex;gap:3px}
.icon{width:27px;height:27px;border:1px solid transparent;background:transparent;border-radius:7px;color:var(--muted);font-weight:800}
.icon:hover{background:var(--panel-2);border-color:var(--line);color:var(--text)}
.head{display:flex;gap:9px;align-items:flex-start;margin-bottom:4px;padding-right:56px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);margin-top:6px;flex:none;box-shadow:0 0 0 4px color-mix(in srgb,var(--ok) 16%,transparent)}
.dot.d{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 16%,transparent)}
.who{min-width:0}
.name{font-weight:800;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub2{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.offline .sub2{color:var(--bad)}
.note{font-size:12px;color:var(--muted);margin:2px 0 0 18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{display:flex;gap:5px;flex-wrap:wrap;margin:6px 0 0 18px}
.tag{font:700 11px/20px var(--mono);height:20px;border-radius:6px;padding:0 7px;background:var(--panel-2);color:var(--muted)}
.tag.g{background:color-mix(in srgb,var(--blue) 13%,var(--panel));color:var(--blue)}
.gauges{display:flex;justify-content:space-around;margin:12px 0 4px}
.g{text-align:center}
.g span{display:block;font-size:11px;color:var(--muted);font-weight:700;margin-top:1px}
.gt{fill:none;stroke:var(--panel-2);stroke-width:7}
.ga{fill:none;stroke-width:7;stroke-linecap:round}
.gv{font:700 15px var(--mono);fill:var(--text);text-anchor:middle}
.gm{fill:var(--muted)}
.net{display:flex;gap:12px;align-items:baseline;font:700 12px var(--mono);color:var(--text);margin:8px 0 0;flex-wrap:wrap}
.net .tot{margin-left:auto;color:var(--muted);font-weight:600}
.ping{display:flex;gap:6px;margin-top:9px;min-height:22px;overflow:hidden;flex-wrap:wrap}
.pill{font:700 11px var(--mono);border:1px solid var(--line);border-radius:999px;padding:3px 8px;color:var(--muted);white-space:nowrap}
.pill.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,var(--line))}
.pill.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,var(--line))}
.stab{margin-top:11px}
.rail{display:flex;gap:2px}
.seg{flex:1;height:14px;border-radius:3px;background:var(--ok)}
.seg.b{background:var(--bad)}
.seg.e{background:var(--line)}
.stabmeta{display:flex;justify-content:space-between;align-items:baseline;margin-top:5px;font-size:11px;color:var(--muted)}
.stabmeta b{font:700 12px var(--mono);color:var(--text)}
.empty{grid-column:1/-1;border:1px dashed var(--line);border-radius:14px;min-height:220px;display:grid;place-items:center;color:var(--muted);background:var(--panel)}
.footer{color:var(--muted);font:600 11px var(--mono);text-align:center;padding:22px 0 4px}
.modal{position:fixed;inset:0;background:rgba(10,14,12,.45);display:none;align-items:center;justify-content:center;padding:18px;z-index:50}
.modal.open{display:flex}
.dialog{width:min(680px,100%);max-height:min(740px,calc(100vh - 36px));overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 24px 70px rgba(0,0,0,.3);padding:16px}
.dialog-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.dialog h2{margin:0;font-size:17px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.field{display:grid;gap:5px}
.field.full{grid-column:1/-1}
.field label{font-size:12px;color:var(--muted);font-weight:800}
.field input,.field select,.field textarea{border:1px solid var(--line);background:var(--panel-2);border-radius:9px;min-height:37px;padding:8px}
.field textarea{resize:vertical;min-height:68px}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px}
.task-list{display:grid;gap:8px;margin-top:14px}
.task{border:1px solid var(--line);border-radius:10px;padding:10px;display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center}
.task b{display:block}
.task small{color:var(--muted);font-family:var(--mono)}
.dialog.wide{width:min(900px,100%)}
.dstatus{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin:-8px 0 12px 0}
.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.info{background:var(--panel-2);border-radius:9px;padding:8px 10px;min-width:0}
.info small{display:block;font-size:11px;color:var(--muted);font-weight:800}
.info b{font:700 12px/1.5 var(--mono);word-break:break-all}
.chart-box{margin-top:14px}
.chart-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:5px;font-size:12px;font-weight:800;color:var(--muted)}
.legend{display:flex;gap:12px;font:700 11px var(--mono);color:var(--muted);flex-wrap:wrap}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
.chart{width:100%;height:auto;display:block;background:var(--panel-2);border-radius:9px}
.axis{font:600 9.5px var(--mono);fill:var(--muted)}
.gridline{stroke:var(--line);stroke-width:1}
.diskrow{display:flex;align-items:center;gap:8px;font:600 12px var(--mono);margin-top:7px;color:var(--muted)}
.diskrow .mnt{min-width:90px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.diskbar{flex:1;height:8px;border-radius:4px;background:var(--panel-2);overflow:hidden}
.diskbar i{display:block;height:100%;border-radius:4px}
.chart-tip{position:fixed;z-index:70;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:7px 10px;font:700 11px var(--mono);box-shadow:var(--shadow);display:none;line-height:1.8}
.chart-tip .tt{color:var(--muted)}
.chart-tip .tr{display:flex;align-items:center;gap:6px;white-space:nowrap}
.chart-tip .tr i{width:8px;height:8px;border-radius:2px;flex:none}
.chart-tip .tr b{margin-left:auto;padding-left:12px}
@media(max-width:860px){.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.hero{flex-direction:row}}
@media(max-width:680px){.app{padding-top:10px}.clock{display:none}.hero{flex-direction:column;align-items:stretch;text-align:center}.ring{margin:0 auto}.grid{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}}
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand"><div class="mark">SP</div><div><h1>小鸡儿</h1><div class="sub" id="brand-sub">VPS 状态墙</div></div></div>
    <div class="actions-top"><span class="clock" id="clock">--:--:--</span><button class="btn" type="button" onclick="openPing()">探测</button><button class="btn" type="button" onclick="setTokenPrompt()" title="设置访问令牌">🔑</button><button class="btn" type="button" onclick="toggleTheme()">主题</button></div>
  </header>
  <section class="hero">
    <svg class="ring" viewBox="0 0 128 128" width="118" height="118" aria-hidden="true">
      <circle class="rt" cx="64" cy="64" r="52"></circle>
      <circle class="ra" id="hero-arc" cx="64" cy="64" r="52" transform="rotate(-90 64 64)" stroke-dasharray="0 326.8"></circle>
      <text class="rv" id="hero-pct" x="64" y="60">--</text>
      <text class="rl" x="64" y="82">在线率</text>
    </svg>
    <div class="tiles" id="tiles"></div>
  </section>
  <section class="toolbar">
    <div class="filters" id="filters"></div>
    <select class="select" id="sort-sel" onchange="refreshView()"><option value="weight">权重排序</option><option value="status">在线优先</option><option value="name">名称</option><option value="cpu">CPU</option><option value="mem">内存</option><option value="disk">磁盘</option><option value="down">下载</option><option value="up">上传</option></select>
  </section>
  <main class="grid" id="servers"></main>
  <footer class="footer">ServerProbe · <span id="ri">3</span>s refresh</footer>
</div>
<div class="modal" id="modal"><div class="dialog" id="dialog"></div></div>
<script>
const R=3,AUTH_REQUIRED=document.querySelector('meta[name="probe-auth-required"]').content==='true';let TOKEN=localStorage.getItem('probe-token')||'',allData={},activeFilter='all',pingTasks=[],refreshTimer=null;
function setTokenPrompt(){const t=prompt('请输入访问 Token（管理员令牌，留空则清除）',TOKEN||'');if(t!==null){TOKEN=t.trim();localStorage.setItem('probe-token',TOKEN);startRefresh();go()}}
function startRefresh(){if(!refreshTimer)refreshTimer=setInterval(go,R*1000)}
function stopRefresh(){if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null}}
function esc(v){const d=document.createElement('div');d.textContent=String(v??'');return d.innerHTML}
function attr(v){return esc(v).replace(/"/g,'&quot;')}
async function jsonFetch(path,opts={}){opts.headers=Object.assign({'Content-Type':'application/json'},opts.headers||{});if(TOKEN)opts.headers['Authorization']='Bearer '+TOKEN;const r=await fetch(path,opts);if(r.status===401||r.status===403){stopRefresh();const j0=await r.json().catch(()=>({}));throw new Error(j0.message||j0.error||'unauthorized')}const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.message||j.error||r.statusText);return j}
function bytes(v){v=Number(v)||0;if(v<=0)return'0 B';const u=['B','KiB','MiB','GiB','TiB','PiB'];const i=Math.min(Math.floor(Math.log(v)/Math.log(1024)),u.length-1);return(v/Math.pow(1024,i)).toFixed(i?1:0)+' '+u[i]}
function speed(v){v=Number(v)||0;if(v<=0)return'0 B/s';const u=['B/s','KiB/s','MiB/s','GiB/s'];const i=Math.min(Math.floor(Math.log(v)/Math.log(1024)),u.length-1);return(v/Math.pow(1024,i)).toFixed(i?1:0)+' '+u[i]}
function pct(v){return Math.max(0,Math.min(100,Number(v)||0))}
function color(v){v=pct(v);return v<65?'var(--ok)':v<85?'var(--warn)':'var(--bad)'}
function ago(ts){const s=Math.max(0,Math.floor(Date.now()/1000-(Number(ts)||0)));if(s<60)return s+'秒前';if(s<3600)return Math.floor(s/60)+'分钟前';if(s<86400)return Math.floor(s/3600)+'小时前';return Math.floor(s/86400)+'天前'}
function mainDisk(disks){return Array.isArray(disks)&&disks.length?(disks.find(d=>d.mount==='/')||disks[0]):{usage_percent:0,used:0,total:0,mount:'/'}}
function netTotals(d){let rx=0,tx=0,rs=0,ts=0;for(const n of (d.network||[])){rx+=Number(n.rx_bytes)||0;tx+=Number(n.tx_bytes)||0;const s=(d.network_speeds||{})[n.interface]||{};rs+=Number(s.rx_speed)||0;ts+=Number(s.tx_speed)||0}return{rx,tx,rs,ts}}
function labelOf(s){return s.meta?.display_name||s.data?.name||s.name||'unknown'}
function dayText(ts){return new Date(ts*1000).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})}
const CIRC=163.4;
function gauge(v,label,off){if(off)return`<div class="g"><svg viewBox="0 0 74 74" width="70" height="70"><circle class="gt" cx="37" cy="37" r="26"></circle><text class="gv gm" x="37" y="42">—</text></svg><span>${label}</span></div>`;const p=pct(v);const on=(p/100*CIRC).toFixed(1);return`<div class="g"><svg viewBox="0 0 74 74" width="70" height="70"><circle class="gt" cx="37" cy="37" r="26"></circle><circle class="ga" cx="37" cy="37" r="26" stroke="${color(p)}" stroke-dasharray="${on} ${CIRC}" transform="rotate(-90 37 37)"></circle><text class="gv" x="37" y="42">${p.toFixed(0)}%</text></svg><span>${label}</span></div>`}
function rail(st,on){const days=Array.isArray(st?.days)?st.days:[];const pv=st?.days_percent??st?.percent;const list=days.length?days:Array.from({length:90},(_,i)=>({ts:Math.floor(Date.now()/86400000)*86400-(89-i)*86400,total:0,ratio:null}));const cells=list.map(d=>{const cls=!d.total?'seg e':(d.ratio>=99?'seg':'seg b');const t=d.total?`${dayText(d.ts)} ${d.ratio.toFixed(2)}%`:`${dayText(d.ts)} 无采样`;return`<i class="${cls}" title="${t}"></i>`}).join('');return`<div class="stab"><div class="rail">${cells}</div><div class="stabmeta"><span>近90天在线</span><b>${pv==null?'--':pv.toFixed(2)+'%'}</b></div></div>`}
function pingHtml(s){const rows=s.ping?.last||[];if(!rows.length)return'<div class="ping"><span class="pill">未配置探测</span></div>';return'<div class="ping">'+rows.slice(0,3).map(r=>`<span class="pill ${r.ok?'ok':'bad'}">${esc(r.task_name||r.target||'ping')} ${r.ok?(Number(r.latency_ms)||0).toFixed(0)+'ms':'失败'}</span>`).join('')+'</div>'}
function renderSummary(list){const visible=list.filter(s=>!s.meta?.hidden),online=visible.filter(s=>s.online).length,total=visible.length,offline=total-online;let rx=0,tx=0,rs=0,ts=0;for(const s of visible){const n=netTotals(s.data||{});rx+=n.rx;tx+=n.tx;rs+=n.rs;ts+=n.ts}const p=total?online/total*100:0;const arc=document.getElementById('hero-arc');arc.setAttribute('stroke-dasharray',(p/100*326.8).toFixed(1)+' 326.8');arc.style.stroke=p>=90?'var(--ok)':p>=60?'var(--warn)':'var(--bad)';document.getElementById('hero-pct').textContent=total?Math.round(p)+'%':'--';document.getElementById('brand-sub').textContent=total?`${total} 节点 · ${online} 在线`:'VPS 状态墙';document.getElementById('tiles').innerHTML=`<div class="tile"><b>${total}</b><span>节点</span></div><div class="tile"><b style="color:var(--ok)">${online}</b><span>在线</span></div><div class="tile"><b style="color:${offline?'var(--bad)':'var(--muted)'}">${offline}</b><span>离线</span></div><div class="tile"><b>${bytes(rx+tx)}</b><span>总流量</span></div><div class="tile wide"><b style="font-size:15px">↑ ${speed(ts)}</b><span>合计上传</span></div><div class="tile wide"><b style="font-size:15px">↓ ${speed(rs)}</b><span>合计下载</span></div>`}
function renderFilters(list){const groups=new Set();for(const s of list){if(s.meta?.group)groups.add(s.meta.group);for(const t of (s.data?.tags||[]))groups.add(t)}const base=[['all','全部'],['online','在线'],['offline','离线'],['hidden','隐藏']];let html=base.map(([id,label])=>`<button class="chip ${activeFilter===id?'active':''}" data-filter="${id}" type="button">${label}</button>`).join('');for(const g of [...groups].sort())html+=`<button class="chip ${activeFilter===g?'active':''}" data-filter="${attr(g)}" type="button">${esc(g)}</button>`;document.getElementById('filters').innerHTML=html}
function card(s){const d=s.data||{},m=s.meta||{},disk=mainDisk(d.disk),net=netTotals(d),cpu=pct(d.cpu?.usage),mem=pct(d.memory?.usage_percent),du=pct(disk.usage_percent),tags=[m.group,...(d.tags||[])].filter(Boolean).slice(0,4);return`<article class="card ${s.online?'':'offline'}" data-name="${attr(s.name)}" title="点击查看详情"><div class="acts"><button class="icon" data-action="edit" data-name="${attr(s.name)}" title="编辑">✎</button><button class="icon" data-action="delete" data-name="${attr(s.name)}" title="删除">×</button></div><div class="head"><span class="dot${s.online?'':' d'}"></span><div class="who"><div class="name" title="${attr(labelOf(s))}">${esc(labelOf(s))}</div><div class="sub2">${esc(d.name||s.name)} · ${s.online?'刚刚更新':'离线 '+ago(s.last_seen)}</div></div></div>${m.note||d.os?.distro?`<div class="note">${esc(m.note||d.os?.distro||'')}</div>`:''}${tags.length?`<div class="tags">${tags.map((t,i)=>`<span class="tag${i===0&&m.group?' g':''}">${esc(t)}</span>`).join('')}</div>`:''}<div class="gauges">${gauge(cpu,'CPU',!s.online)}${gauge(mem,'内存',!s.online)}${gauge(du,'磁盘',!s.online)}</div><div class="net"><span>↑ ${speed(net.ts)}</span><span>↓ ${speed(net.rs)}</span><span class="tot">累计 ↑${bytes(net.tx)} ↓${bytes(net.rx)}</span></div>${pingHtml(s)}${rail(s.stability,s.online)}</article>`}
function refreshView(){const list=Object.values(allData);renderSummary(list);renderFilters(list);let rows=list.filter(s=>activeFilter==='hidden'?s.meta?.hidden:!s.meta?.hidden);if(activeFilter==='online')rows=rows.filter(s=>s.online);else if(activeFilter==='offline')rows=rows.filter(s=>!s.online);else if(!['all','hidden'].includes(activeFilter))rows=rows.filter(s=>s.meta?.group===activeFilter||(s.data?.tags||[]).includes(activeFilter));const sort=document.getElementById('sort-sel').value;rows=[...rows].sort((a,b)=>{const ad=a.data||{},bd=b.data||{};if(sort==='status')return Number(b.online)-Number(a.online);if(sort==='cpu')return (bd.cpu?.usage||0)-(ad.cpu?.usage||0);if(sort==='mem')return (bd.memory?.usage_percent||0)-(ad.memory?.usage_percent||0);if(sort==='disk')return mainDisk(bd.disk).usage_percent-mainDisk(ad.disk).usage_percent;if(sort==='down')return netTotals(bd).rs-netTotals(ad).rs;if(sort==='up')return netTotals(bd).ts-netTotals(ad).ts;if(sort==='name')return labelOf(a).localeCompare(labelOf(b),'zh-CN');return (b.meta?.weight||0)-(a.meta?.weight||0)||labelOf(a).localeCompare(labelOf(b),'zh-CN')});document.getElementById('servers').innerHTML=rows.length?rows.map(card).join(''):'<div class="empty">暂无匹配节点</div>'}
async function go(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});try{allData=await jsonFetch('/api/servers');refreshView()}catch(e){console.error(e)}}
let detailTimer=null,detailName=null;
function closeModal(){document.getElementById('modal').classList.remove('open');document.getElementById('dialog').classList.remove('wide');if(detailTimer){clearInterval(detailTimer);detailTimer=null}detailName=null}
function showModal(html,wide){const dg=document.getElementById('dialog');dg.innerHTML=html;dg.classList.toggle('wide',!!wide);document.getElementById('modal').classList.add('open')}
function niceMax(v){if(v<=0)return 1;const e=Math.pow(10,Math.floor(Math.log10(v)));const n=v/e;return (n<=1?1:n<=2?2:n<=5?5:10)*e}
function fmtAxis(v,kind){if(kind==='speed')return speed(v).replace(/ /,'');if(kind==='ms')return Math.round(v)+'ms';return Math.round(v)+'%'}
function fmtUptime(s){s=Number(s)||0;const d=Math.floor(s/86400),h=Math.floor(s%86400/3600);return d?d+'天'+h+'小时':h?h+'小时'+Math.floor(s%3600/60)+'分':Math.floor(s/60)+'分钟'}
let CHARTS={},chartSeq=0;
function chartSVG(seriesList,opts={}){const W=600,H=150,PL=40,PR=8,PT=8,PB=17,iw=W-PL-PR,ih=H-PT-PB;const now=Math.floor(Date.now()/1000),span=(opts.hours||24)*3600,t0=now-span;let max=opts.max;if(max==null){let peak=0;for(const s of seriesList)for(const p of s.pts)if(p.v!=null&&p.v>peak)peak=p.v;max=niceMax(peak*1.12||1)}const X=ts=>PL+Math.max(0,Math.min(1,(ts-t0)/span))*iw,Y=v=>PT+ih-Math.max(0,Math.min(max,v))/max*ih;let body='';for(let i=0;i<=2;i++){const y=PT+ih*i/2;body+=`<line class="gridline" x1="${PL}" y1="${y.toFixed(1)}" x2="${W-PR}" y2="${y.toFixed(1)}"></line><text class="axis" x="${PL-5}" y="${(y+3).toFixed(1)}" text-anchor="end">${fmtAxis(max*(1-i/2),opts.fmt)}</text>`}for(let i=0;i<=4;i++){const ts=t0+span*i/4;body+=`<text class="axis" x="${X(ts).toFixed(1)}" y="${H-4}" text-anchor="${i===0?'start':i===4?'end':'middle'}">${new Date(ts*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</text>`}for(const s of seriesList){let d='',pen=false;for(const p of s.pts){if(p.v==null){pen=false;continue}d+=(pen?'L':'M')+X(p.ts).toFixed(1)+' '+Y(p.v).toFixed(1);pen=true}if(d)body+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.6" stroke-linejoin="round"></path>`;if(s.failDots)for(const p of s.pts)if(p.fail)body+=`<circle cx="${X(p.ts).toFixed(1)}" cy="${(PT+ih-3).toFixed(1)}" r="2.4" fill="var(--bad)"></circle>`}const cid='c'+(++chartSeq);CHARTS[cid]={series:seriesList,t0,span,max,fmt:opts.fmt,PL,iw,PT,ih};return`<svg class="chart" data-cid="${cid}" viewBox="0 0 ${W} ${H}" role="img">${body}<line class="guide" x1="0" x2="0" y1="${PT}" y2="${PT+ih}" stroke="var(--muted)" stroke-dasharray="3 3" style="display:none"></line></svg>`}
function chartBox(title,legend,svg){return`<div class="chart-box"><div class="chart-head"><span>${title}</span><span class="legend">${legend}</span></div>${svg}</div>`}
const TASK_COLORS=['var(--blue)','var(--ok)','var(--warn)','var(--bad)'];
async function openDetail(name){detailName=name;showModal(`<div class="dialog-head"><h2>${esc(labelOf(allData[name]||{name}))}</h2><button class="icon" onclick="closeModal()">×</button></div><div class="empty" style="min-height:120px">加载中...</div>`,true);try{await renderDetail(name)}catch(e){if(detailName===name)document.getElementById('dialog').innerHTML=`<div class="dialog-head"><h2>加载失败</h2><button class="icon" onclick="closeModal()">×</button></div><div class="empty" style="min-height:80px">${esc(e.message)}</div>`}if(detailTimer)clearInterval(detailTimer);detailTimer=setInterval(()=>{if(detailName)renderDetail(detailName).catch(()=>{})},10000)}
async function renderDetail(name){const j=await jsonFetch('/api/server/detail?name='+encodeURIComponent(name));if(detailName!==name)return;CHARTS={};chartSeq=0;const d=j.data||{},m=j.meta||{},hours=j.hours||24,metrics=j.metrics||[];const title=m.display_name||d.name||name;const cpuPts=metrics.map(r=>({ts:r.ts,v:r.cpu})),memPts=metrics.map(r=>({ts:r.ts,v:r.mem})),txPts=metrics.map(r=>({ts:r.ts,v:r.tx})),rxPts=metrics.map(r=>({ts:r.ts,v:r.rx}));const pingSeries=(j.ping_tasks||[]).slice(0,4).map((t,i)=>({color:TASK_COLORS[i%TASK_COLORS.length],failDots:true,label:t.name,pts:t.rows.map(r=>({ts:r.ts,v:r.ok?Number(r.latency_ms)||0:null,fail:!r.ok}))}));const disks=(Array.isArray(d.disk)?d.disk:[]).slice(0,6);const info=[['系统',(d.os?.distro||d.os?.os||'--')],['内核',d.os?.kernel||'--'],['CPU',(d.cpu?.model||'--')+(d.cpu?.cores?' · '+d.cpu.cores+'核':'')],['虚拟化',d.virtualization||'--'],['在线时长',fmtUptime(d.load?.uptime)],['负载',[d.load?.load_1,d.load?.load_5,d.load?.load_15].map(x=>x==null?'-':Number(x).toFixed(2)).join(' / ')],['内存',bytes(d.memory?.used)+' / '+bytes(d.memory?.total)],['上报间隔',(d.report_interval||'--')+'s']];let html=`<div class="dialog-head"><h2>${esc(title)}</h2><button class="icon" onclick="closeModal()">×</button></div><div class="dstatus"><span class="dot${j.online?'':' d'}" style="margin:0"></span><span>${j.online?'在线':'离线 '+ago(j.last_seen)}</span><span>·</span><span>${esc(d.name||name)}</span>${m.group?`<span>·</span><span>${esc(m.group)}</span>`:''}${m.note?`<span>·</span><span>${esc(m.note)}</span>`:''}</div><div class="info-grid">${info.map(([k,v])=>`<div class="info"><small>${k}</small><b>${esc(v)}</b></div>`).join('')}</div>`;if(!metrics.length){html+='<div class="chart-box"><div class="empty" style="min-height:90px">暂无历史采样，等待 Agent 上报积累</div></div>'}else{html+=chartBox(`CPU / 内存 · 近${hours}小时`,`<span><i style="background:var(--blue)"></i>CPU</span><span><i style="background:var(--ok)"></i>内存</span>`,chartSVG([{color:'var(--blue)',label:'CPU',pts:cpuPts},{color:'var(--ok)',label:'内存',pts:memPts}],{max:100,hours}));html+=chartBox(`网络速率 · 近${hours}小时`,`<span><i style="background:var(--warn)"></i>上传</span><span><i style="background:var(--blue)"></i>下载</span>`,chartSVG([{color:'var(--warn)',label:'上传',pts:txPts},{color:'var(--blue)',label:'下载',pts:rxPts}],{fmt:'speed',hours}))}if(pingSeries.length){html+=chartBox(`探测延迟 · 近${hours}小时`,pingSeries.map(s=>`<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('')+'<span><i style="background:var(--bad);border-radius:50%"></i>失败</span>',chartSVG(pingSeries,{fmt:'ms',hours}))}if(disks.length){html+=`<div class="chart-box"><div class="chart-head"><span>磁盘分区</span></div>${disks.map(dk=>{const p=pct(dk.usage_percent);return`<div class="diskrow"><span class="mnt" title="${attr(dk.mount)}">${esc(dk.mount)}</span><div class="diskbar"><i style="width:${p}%;background:${color(p)}"></i></div><span>${p.toFixed(0)}% · ${bytes(dk.used)}/${bytes(dk.total)}</span></div>`}).join('')}</div>`}html+=rail(j.stability,j.online);document.getElementById('dialog').innerHTML=html}
function openMeta(name){const s=allData[name],m=s?.meta||{};showModal(`<div class="dialog-head"><h2>编辑节点</h2><button class="icon" onclick="closeModal()">×</button></div><div class="form-grid"><input id="meta-name" type="hidden" value="${attr(name)}"><div class="field"><label>展示名称</label><input id="meta-display" value="${attr(m.display_name||'')}"></div><div class="field"><label>分组</label><input id="meta-group" value="${attr(m.group||'')}"></div><div class="field"><label>排序权重</label><input id="meta-weight" type="number" value="${Number(m.weight)||0}"></div><div class="field"><label>隐藏</label><select id="meta-hidden"><option value="false" ${m.hidden?'':'selected'}>显示</option><option value="true" ${m.hidden?'selected':''}>隐藏</option></select></div><div class="field full"><label>备注</label><textarea id="meta-note">${esc(m.note||'')}</textarea></div></div><p class="row"><button class="btn primary" onclick="saveMeta()">保存更改</button><button class="btn" onclick="closeModal()">取消</button></p>`)}
async function saveMeta(){const name=document.getElementById('meta-name').value;await jsonFetch('/api/server/meta',{method:'POST',body:JSON.stringify({name,display_name:document.getElementById('meta-display').value,group:document.getElementById('meta-group').value,note:document.getElementById('meta-note').value,weight:Number(document.getElementById('meta-weight').value)||0,hidden:document.getElementById('meta-hidden').value==='true'})});closeModal();await go()}
async function openPing(){showModal(`<div class="dialog-head"><h2>探测任务</h2><button class="icon" onclick="closeModal()">×</button></div><div class="form-grid"><input id="task-id" type="hidden"><div class="field"><label>名称</label><input id="task-name" placeholder="API 延迟"></div><div class="field"><label>类型</label><select id="task-type"><option value="tcp">TCP</option><option value="http">HTTP</option><option value="icmp">ICMP</option></select></div><div class="field full"><label>目标</label><input id="task-target" placeholder="example.com:443 或 https://example.com"></div><div class="field"><label>执行节点</label><select id="task-server"><option value="">全部节点</option>${Object.values(allData).map(s=>`<option value="${attr(s.name)}">${esc(labelOf(s))}</option>`).join('')}</select></div><div class="field"><label>间隔/秒</label><input id="task-interval" type="number" min="10" value="60"></div><div class="field"><label>状态</label><select id="task-enabled"><option value="true">启用</option><option value="false">停用</option></select></div></div><p class="row"><button class="btn primary" onclick="savePingTask()">保存任务</button><button class="btn" onclick="resetPingForm()">新建</button></p><div class="task-list" id="task-list">加载中...</div>`);await loadPingTasks()}
async function loadPingTasks(){const j=await jsonFetch('/api/ping-tasks');pingTasks=j.tasks||[];document.getElementById('task-list').innerHTML=pingTasks.length?pingTasks.map(t=>`<div class="task"><div><b>${esc(t.name||t.target)}</b><small>${esc(t.type)} · ${esc(t.target)} · ${t.server?esc(t.server):'全部节点'} · ${t.interval}s · ${t.enabled?'启用':'停用'}</small></div><div class="row" style="margin:0"><button class="btn" onclick="editPingTask('${attr(t.id)}')">编辑</button><button class="btn" onclick="deletePingTask('${attr(t.id)}')">删除</button></div></div>`).join(''):'<div class="empty" style="min-height:90px">还没有探测任务</div>'}
function resetPingForm(){for(const id of ['task-id','task-name','task-target'])document.getElementById(id).value='';document.getElementById('task-type').value='tcp';document.getElementById('task-server').value='';document.getElementById('task-interval').value=60;document.getElementById('task-enabled').value='true'}
function editPingTask(id){const t=pingTasks.find(x=>x.id===id);if(!t)return;document.getElementById('task-id').value=t.id;document.getElementById('task-name').value=t.name||'';document.getElementById('task-type').value=t.type||'tcp';document.getElementById('task-target').value=t.target||'';document.getElementById('task-server').value=t.server||'';document.getElementById('task-interval').value=t.interval||60;document.getElementById('task-enabled').value=String(t.enabled!==false)}
async function savePingTask(){await jsonFetch('/api/ping-task',{method:'POST',body:JSON.stringify({id:document.getElementById('task-id').value,name:document.getElementById('task-name').value,type:document.getElementById('task-type').value,target:document.getElementById('task-target').value,server:document.getElementById('task-server').value,interval:Number(document.getElementById('task-interval').value)||60,enabled:document.getElementById('task-enabled').value==='true'})});resetPingForm();await loadPingTasks()}
async function deletePingTask(id){await jsonFetch('/api/ping-task?id='+encodeURIComponent(id),{method:'DELETE'});await loadPingTasks()}
async function deleteServer(name,btn){if(!btn.dataset.confirm){btn.dataset.confirm='1';btn.textContent='!';setTimeout(()=>{btn.dataset.confirm='';btn.textContent='×'},2200);return}await jsonFetch('/api/server?name='+encodeURIComponent(name),{method:'DELETE'});await go()}
function toggleTheme(){const dark=document.documentElement.getAttribute('data-theme')==='dark';if(dark){document.documentElement.removeAttribute('data-theme');localStorage.setItem('probe-theme','light')}else{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('probe-theme','dark')}}
document.getElementById('filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;activeFilter=b.dataset.filter;refreshView()});
document.getElementById('servers').addEventListener('click',e=>{const b=e.target.closest('[data-action]');if(b){if(b.dataset.action==='edit')openMeta(b.dataset.name);if(b.dataset.action==='delete')deleteServer(b.dataset.name,b);return}const c=e.target.closest('.card');if(c&&c.dataset.name)openDetail(c.dataset.name)});
document.getElementById('modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal()});
const tipEl=document.createElement('div');tipEl.className='chart-tip';document.body.appendChild(tipEl);
function hideTip(){if(tipEl.style.display!=='none'){tipEl.style.display='none';document.querySelectorAll('svg.chart .guide').forEach(g=>g.style.display='none')}}
function nearestPt(pts,t){if(!pts||!pts.length)return null;let lo=0,hi=pts.length-1;while(hi-lo>1){const mid=(lo+hi)>>1;if(pts[mid].ts<t)lo=mid;else hi=mid}const a=pts[lo],b=pts[hi];return Math.abs(a.ts-t)<=Math.abs(b.ts-t)?a:b}
function fmtVal(v,kind){if(kind==='speed')return speed(v);if(kind==='ms')return (Math.round(v*10)/10)+'ms';return Number(v).toFixed(1)+'%'}
document.addEventListener('pointermove',e=>{const svg=e.target&&e.target.closest?e.target.closest('svg.chart'):null;if(!svg||!svg.dataset.cid||!CHARTS[svg.dataset.cid]){hideTip();return}const c=CHARTS[svg.dataset.cid];const r=svg.getBoundingClientRect();if(!r.width)return;const vx=(e.clientX-r.left)*600/r.width;const frac=Math.max(0,Math.min(1,(vx-c.PL)/c.iw));const t=c.t0+frac*c.span;const tol=Math.max(120,c.span/120);let rows=[],anchor=null;for(const s of c.series){const p=nearestPt(s.pts,t);if(!p||Math.abs(p.ts-t)>tol)continue;if(anchor===null||Math.abs(p.ts-t)<Math.abs(anchor-t))anchor=p.ts;rows.push({s,p})}if(!rows.length){hideTip();return}const g=svg.querySelector('.guide');if(g){const gx=c.PL+Math.max(0,Math.min(1,(anchor-c.t0)/c.span))*c.iw;g.setAttribute('x1',gx.toFixed(1));g.setAttribute('x2',gx.toFixed(1));g.style.display=''}
tipEl.innerHTML=`<div class="tt">${new Date(anchor*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</div>`+rows.map(({s,p})=>`<div class="tr"><i style="background:${s.color}"></i>${esc(s.label||'')}${p.v==null?(p.fail?'<b style="color:var(--bad)">失败</b>':'<b>—</b>'):`<b>${fmtVal(p.v,c.fmt)}</b>`}</div>`).join('');
tipEl.style.display='block';const tw=tipEl.offsetWidth,th=tipEl.offsetHeight;let lx=e.clientX+14,ly=e.clientY+12;if(lx+tw>innerWidth-8)lx=e.clientX-tw-14;if(ly+th>innerHeight-8)ly=e.clientY-th-12;tipEl.style.left=lx+'px';tipEl.style.top=ly+'px'});
document.addEventListener('scroll',hideTip,true);
const themePref=localStorage.getItem('probe-theme');if(themePref==='dark'||(!themePref&&window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches))document.documentElement.setAttribute('data-theme','dark');
document.getElementById('ri').textContent=R;if(AUTH_REQUIRED&&!TOKEN){setTokenPrompt()}else{go();startRefresh()}
</script>
</body>
</html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _authorized(self):
        if not AUTH_TOKEN:
            return True
        auth = self.headers.get('Authorization', '')
        token = parse_qs(urlparse(self.path).query).get('token', [''])[0]
        return auth == f'Bearer {AUTH_TOKEN}' or token == AUTH_TOKEN

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0 or length > MAX_BODY_SIZE:
            raise ValueError(f'body size must be 1-{MAX_BODY_SIZE} bytes')
        body = self.rfile.read(length)
        if not body:
            raise ValueError('empty body')
        return json.loads(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(HTML_PAGE.replace('__AUTH_REQUIRED__', 'true' if AUTH_TOKEN else 'false').encode('utf-8'))
            return
        if not self._authorized():
            self._send_json({'error': 'unauthorized'}, 403)
            return
        if path == '/api/servers':
            now = time.time()
            result = {}
            with lock:
                for name, srv in servers.items():
                    result[name] = {
                        'name': name,
                        'data': srv['data'],
                        'meta': normalize_meta(server_meta.get(name)),
                        'online': now - srv['last_seen'] < OFFLINE_TIMEOUT,
                        'last_seen': srv['last_seen'],
                        'stability': stability_payload(name, now),
                        'ping': ping_summary_for_server(name),
                    }
            self._send_json(result)
            return
        if path == '/api/server/detail':
            qs = parse_qs(urlparse(self.path).query)
            name = qs.get('name', [None])[0]
            now = time.time()
            with lock:
                srv = servers.get(name)
                if not srv:
                    self._send_json({'error': 'not found'}, 404)
                    return
                cutoff = now - METRICS_HISTORY_HOURS * 3600
                metrics = [r for r in metrics_history.get(name, []) if r.get('ts', 0) >= cutoff]
                tasks_out = []
                for task in ping_tasks:
                    rows = [
                        {'ts': r.get('ts'), 'ok': bool(r.get('ok')), 'latency_ms': r.get('latency_ms')}
                        for r in ping_history.get(task.get('id'), [])
                        if r.get('server') == name and (r.get('ts') or 0) >= cutoff
                    ]
                    if rows:
                        tasks_out.append({
                            'id': task.get('id'),
                            'name': task.get('name') or task.get('target'),
                            'type': task.get('type'),
                            'target': task.get('target'),
                            'rows': rows[-720:],
                        })
                payload = {
                    'name': name,
                    'data': srv['data'],
                    'meta': normalize_meta(server_meta.get(name)),
                    'online': now - srv['last_seen'] < OFFLINE_TIMEOUT,
                    'last_seen': srv['last_seen'],
                    'stability': stability_payload(name, now),
                    'metrics': metrics,
                    'ping_tasks': tasks_out,
                    'hours': METRICS_HISTORY_HOURS,
                }
            self._send_json(payload)
            return
        if path == '/api/ping-tasks':
            with lock:
                prune_ping_history()
                self._send_json({'tasks': list(ping_tasks), 'history': dict(ping_history)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/report':
            if AUTH_TOKEN and self.headers.get('Authorization', '') != f'Bearer {AUTH_TOKEN}':
                self._send_json({'status': 'error', 'message': 'unauthorized'}, 403)
                return
            try:
                raw = self._read_json()
                now = time.time()
                raw_basic = raw.get('basic') if isinstance(raw.get('basic'), dict) else {}
                name = raw.get('name') or raw_basic.get('name') or raw_basic.get('hostname') or 'unknown'
                with lock:
                    previous = servers.get(name, {}).get('data', {})
                    data = normalize_report(raw, previous)
                    data['name'] = name
                    if not data.get('report_interval'):
                        data['report_interval'] = raw.get('report_interval')
                    was_known = name in server_status_state
                    was_online = bool(server_status_state.get(name))
                    servers[name] = {'data': data, 'last_seen': now}
                    record_metrics_sample(name, data, now)
                    ingest_ping_results(name, raw.get('ping_results', []), now)
                    evaluate_resource_alerts(name, data, now)
                    if was_known and not was_online:
                        queue_notify(f'{display_name_for(name)} 恢复在线', '已重新收到 Agent 上报。', 'info', name)
                    server_status_state[name] = True
                    tasks = tasks_for_agent(name)
                self._send_json({'status': 'ok', 'ping_tasks': tasks, 'basic_info_interval': BASIC_INFO_INTERVAL})
            except json.JSONDecodeError as e:
                self._send_json({'status': 'error', 'message': f'invalid json: {e}'}, 400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, 400)
            return
        if not self._authorized():
            self._send_json({'status': 'error', 'message': 'unauthorized'}, 403)
            return
        try:
            payload = self._read_json()
            if path == '/api/server/meta':
                name = str(payload.get('name', '') or '').strip()
                if not name:
                    self._send_json({'status': 'error', 'message': 'missing name'}, 400)
                    return
                with lock:
                    current = server_meta.get(name, {})
                    merged = dict(current)
                    for key in ('display_name', 'note', 'group', 'hidden', 'weight'):
                        if key in payload:
                            merged[key] = payload[key]
                    server_meta[name] = normalize_meta(merged)
                self._send_json({'status': 'ok', 'meta': server_meta[name]})
                return
            if path == '/api/ping-task':
                with lock:
                    task = normalize_ping_task(payload)
                    for i, old in enumerate(ping_tasks):
                        if old.get('id') == task['id']:
                            ping_tasks[i] = task
                            break
                    else:
                        ping_tasks.append(task)
                self._send_json({'status': 'ok', 'task': task})
                return
        except json.JSONDecodeError as e:
            self._send_json({'status': 'error', 'message': f'invalid json: {e}'}, 400)
            return
        except Exception as e:
            self._send_json({'status': 'error', 'message': str(e)}, 400)
            return
        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._authorized():
            self._send_json({'status': 'error', 'message': 'unauthorized'}, 403)
            return
        qs = parse_qs(urlparse(self.path).query)
        if path == '/api/server':
            name = qs.get('name', [None])[0]
            if not name:
                self._send_json({'status': 'error', 'message': 'missing name parameter'}, 400)
                return
            with lock:
                if name in servers:
                    del servers[name]
                    stability.pop(name, None)
                    server_meta.pop(name, None)
                    server_status_state.pop(name, None)
                    metrics_history.pop(name, None)
                    self._send_json({'status': 'ok', 'message': f'server {name} deleted'})
                else:
                    self._send_json({'status': 'error', 'message': f'server {name} not found'}, 404)
            return
        if path == '/api/ping-task':
            task_id = qs.get('id', [None])[0]
            if not task_id:
                self._send_json({'status': 'error', 'message': 'missing id parameter'}, 400)
                return
            with lock:
                before = len(ping_tasks)
                ping_tasks[:] = [t for t in ping_tasks if t.get('id') != task_id]
                ping_history.pop(task_id, None)
            self._send_json({'status': 'ok' if len(ping_tasks) != before else 'not_found'})
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    load_persist()
    threading.Thread(target=cleanup_ghosts, daemon=True).start()
    threading.Thread(target=stability_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=notify_loop, daemon=True).start()
    if PERSIST_FILE:
        threading.Thread(target=persist_loop, daemon=True).start()

    print(f'''
ServerProbe Dashboard 启动
   地址: http://0.0.0.0:{PORT}
   离线判定: {OFFLINE_TIMEOUT}秒
   稳定性采样: {f"{STABILITY_SAMPLE_INTERVAL}秒 / 保留{STABILITY_RETENTION_DAYS}天" if STABILITY_SAMPLE_INTERVAL > 0 else "关闭"}
   Ping 历史: {PING_HISTORY_DAYS}天
   告警: {"已配置" if (NOTIFY_WEBHOOK_URL or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)) else "未配置"}
   鉴权: {"已启用" if AUTH_TOKEN else "未启用"}
   持久化: {PERSIST_FILE or "未启用"}
''')
    srv = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        srv.server_close()
