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
        with open(PERSIST_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('version') == 3:
            server_data = data.get('servers', {})
            stability_data = data.get('stability', {})
            meta_data = data.get('server_meta', {})
            task_data = data.get('ping_tasks', [])
            history_data = data.get('ping_history', {})
        elif isinstance(data, dict) and data.get('version') == 2 and isinstance(data.get('servers'), dict):
            server_data = data.get('servers', {})
            stability_data = data.get('stability', {})
            meta_data = {}
            task_data = []
            history_data = {}
        else:
            server_data = data if isinstance(data, dict) else {}
            stability_data = {}
            meta_data = {}
            task_data = []
            history_data = {}
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
                }
                serialized = json.dumps(data, ensure_ascii=False)
            tmp = PERSIST_FILE + '.tmp'
            with open(tmp, 'w') as f:
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
*{box-sizing:border-box}html{font-size:16px}body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font-family:var(--font-body);letter-spacing:0;line-height:1.45}
:root{color-scheme:light;--bg:#F7F8F6;--panel:#FFFFFF;--panel-2:#F0F2EF;--text:#111417;--muted:#747A80;--line:#DFE3DE;--line-2:#C9D0C8;--green:#16B876;--red:#E5484D;--yellow:#F5A524;--blue:#2F6FED;--ink:#111417;--shadow:0 14px 36px rgba(28,36,32,.08);--font-body:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;--font-display:"Arial Narrow","Roboto Condensed","PingFang SC",var(--font-body);--font-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
[data-theme="dark"]{color-scheme:dark;--bg:#101312;--panel:#181C1A;--panel-2:#121614;--text:#F1F4F2;--muted:#9AA39D;--line:#28302B;--line-2:#3B453F;--ink:#F1F4F2;--shadow:0 16px 40px rgba(0,0,0,.35)}
button,input,select,textarea{font:inherit;color:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--blue);outline-offset:2px}.app{width:min(1440px,calc(100vw - 32px));margin:0 auto;padding:18px 0 28px}
.top{height:48px;display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}.brand{display:flex;align-items:center;gap:10px}.mark{width:31px;height:31px;border:2px solid var(--ink);display:grid;place-items:center;font:900 15px/1 var(--font-mono);border-radius:6px;background:var(--panel)}.brand h1{margin:0;font:850 18px/1 var(--font-display);letter-spacing:.02em}.brand small{color:var(--muted);border-left:1px solid var(--line);padding-left:10px}.top-actions{display:flex;align-items:center;gap:8px}.btn{border:1px solid var(--line);background:var(--panel);height:36px;border-radius:8px;padding:0 12px;font-weight:760;box-shadow:0 2px 10px rgba(0,0,0,.03)}.btn:hover{border-color:var(--line-2)}.btn.primary{background:var(--ink);color:var(--bg);border-color:var(--ink)}.clock{font:800 13px var(--font-mono);color:var(--muted)}
.overview{display:grid;grid-template-columns:320px 1fr;gap:12px;margin-bottom:12px}.headline{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;box-shadow:var(--shadow);min-height:148px}.headline .eyebrow{font:800 11px var(--font-mono);color:var(--blue);text-transform:uppercase}.headline .big{font:900 46px/.92 var(--font-display);margin-top:12px;letter-spacing:.01em}.headline p{margin:10px 0 0;color:var(--muted);font-size:13px}.fleet{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;box-shadow:var(--shadow);min-width:0}.fleet-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:12px}.fleet-title{font-weight:850}.fleet-sub{font:760 12px var(--font-mono);color:var(--muted)}.fleet-strip{display:grid;grid-template-columns:repeat(auto-fill,minmax(9px,1fr));gap:4px;align-items:end;min-height:82px}.fleet-seg{height:78px;border-radius:8px;background:var(--green);opacity:.92}.fleet-seg.off{background:var(--red)}.fleet-seg.hidden{opacity:.24}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:12px}.stat{border-top:1px solid var(--line);padding-top:10px}.stat b{display:block;font:900 20px var(--font-mono)}.stat span{font-size:12px;color:var(--muted)}
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:14px 0 12px}.filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.chip{border:1px solid var(--line);background:var(--panel);height:34px;border-radius:8px;padding:0 10px;font-weight:780;color:var(--muted)}.chip.active{color:var(--text);border-color:var(--ink);box-shadow:inset 0 -2px 0 var(--ink)}.tools{display:flex;align-items:center;gap:8px}.select{height:36px;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:0 30px 0 10px;font-weight:760}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 10px 28px rgba(28,36,32,.06);padding:13px;display:grid;grid-template-columns:218px minmax(0,1fr);gap:14px;position:relative;min-height:218px;overflow:hidden}.card.offline{border-color:rgba(229,72,77,.45);box-shadow:inset 3px 0 0 var(--red),0 10px 28px rgba(28,36,32,.06)}.identity{display:grid;grid-template-columns:14px 1fr;gap:8px;align-content:start;min-width:0}.dot{width:10px;height:10px;border-radius:999px;background:var(--green);margin-top:5px;box-shadow:0 0 0 5px color-mix(in srgb,var(--green) 16%,transparent)}.offline .dot{background:var(--red);box-shadow:0 0 0 5px color-mix(in srgb,var(--red) 16%,transparent)}.node-name{font-weight:900;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.node-real{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.note{grid-column:2;font-size:12px;color:var(--muted);min-height:18px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.chips{grid-column:2;display:flex;gap:5px;flex-wrap:wrap;margin-top:3px}.tag{font:800 10px/18px var(--font-mono);height:18px;border-radius:5px;padding:0 6px;background:var(--panel-2);border:1px solid var(--line);color:var(--muted)}.tag.group{background:color-mix(in srgb,var(--blue) 12%,var(--panel));color:var(--blue);border-color:color-mix(in srgb,var(--blue) 24%,var(--line))}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;min-width:0}.metric label{display:block;font-size:11px;color:var(--muted);font-weight:780}.metric strong{display:block;font:900 15px var(--font-mono);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meter{height:4px;background:var(--panel-2);border-radius:999px;margin-top:7px;overflow:hidden}.meter i{display:block;height:100%;border-radius:999px;background:var(--green)}.traffic{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:16px}.traffic div{height:30px;border:1px solid var(--line);border-radius:8px;background:var(--panel-2);display:flex;align-items:center;justify-content:center;font:850 12px var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ping{display:flex;gap:6px;margin-top:10px;min-height:24px;overflow:hidden}.ping-pill{font:800 11px var(--font-mono);border:1px solid var(--line);border-radius:999px;padding:4px 7px;color:var(--muted);white-space:nowrap}.ping-pill.ok{color:var(--green);border-color:color-mix(in srgb,var(--green) 36%,var(--line))}.ping-pill.bad{color:var(--red);border-color:color-mix(in srgb,var(--red) 36%,var(--line))}
.stability{grid-column:1/-1;display:grid;grid-template-columns:138px minmax(0,1fr);gap:12px;align-items:center;margin-top:2px;min-height:30px}.stability b{font:900 16px var(--font-mono)}.stability span{font-size:12px;color:var(--muted)}.rail{display:grid;grid-template-columns:repeat(90,minmax(2px,1fr));gap:3px;min-width:0;max-height:17px;overflow:hidden}.uptime-seg{display:block;height:17px;min-height:17px;max-height:17px;border-radius:999px;background:var(--green)}.uptime-seg.bad{background:var(--red)}.uptime-seg.empty{opacity:.26}.actions{position:absolute;right:10px;top:9px;display:flex;gap:4px}.icon{width:28px;height:28px;border:1px solid transparent;background:transparent;border-radius:7px;color:var(--muted);font-weight:900}.icon:hover{background:var(--panel-2);border-color:var(--line);color:var(--text)}.empty{grid-column:1/-1;border:1px dashed var(--line-2);border-radius:8px;min-height:260px;display:grid;place-items:center;color:var(--muted);background:var(--panel)}
.modal{position:fixed;inset:0;background:rgba(17,20,23,.42);display:none;align-items:center;justify-content:center;padding:18px;z-index:50}.modal.open{display:flex}.dialog{width:min(720px,100%);max-height:min(760px,calc(100vh - 36px));overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 24px 80px rgba(0,0,0,.28);padding:16px}.dialog-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.dialog h2{margin:0;font:900 19px var(--font-display)}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{display:grid;gap:5px}.field.full{grid-column:1/-1}.field label{font-size:12px;color:var(--muted);font-weight:820}.field input,.field select,.field textarea{border:1px solid var(--line);background:var(--panel-2);border-radius:8px;min-height:38px;padding:8px}.field textarea{resize:vertical;min-height:70px}.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.task-list{display:grid;gap:8px;margin-top:14px}.task{border:1px solid var(--line);border-radius:8px;padding:10px;display:grid;grid-template-columns:1fr auto;gap:8px}.task b{display:block}.task small{color:var(--muted);font-family:var(--font-mono)}.footer{color:var(--muted);font:760 11px var(--font-mono);text-align:center;padding:22px 0 4px}
@media(max-width:1080px){.overview{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.fleet-strip{min-height:54px}.fleet-seg{height:50px}}
@media(max-width:680px){.app{width:min(100vw - 20px,1440px);padding-top:10px}.top{align-items:flex-start;height:auto;gap:10px}.brand small,.clock{display:none}.overview{gap:10px}.stats{grid-template-columns:repeat(2,1fr)}.toolbar{align-items:stretch;flex-direction:column}.tools{width:100%;justify-content:space-between}.select{flex:1}.card{grid-template-columns:1fr;min-height:0}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.stability{grid-template-columns:1fr;gap:6px}.rail{gap:2px}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}}
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand"><div class="mark">SP</div><h1>小鸡儿</h1><small>VPS 状态墙</small></div>
    <div class="top-actions"><span class="clock" id="clock">--:--:--</span><button class="btn" type="button" onclick="openPing()">探测</button><button class="btn" type="button" onclick="setTokenPrompt()" title="设置访问令牌">🔑</button><button class="btn" type="button" onclick="toggleTheme()">主题</button></div>
  </header>
  <section class="overview">
    <div class="headline"><div class="eyebrow">Fleet health</div><div class="big" id="headline">--</div><p id="headline-sub">等待 Agent 上报。</p><div class="stats" id="stats"></div></div>
    <div class="fleet"><div class="fleet-head"><div class="fleet-title">节点状态轨道</div><div class="fleet-sub" id="fleet-sub">0 nodes</div></div><div class="fleet-strip" id="fleet-strip"></div></div>
  </section>
  <section class="toolbar">
    <div class="filters" id="filters"></div>
    <div class="tools"><select class="select" id="sort-sel" onchange="refreshView()"><option value="weight">权重排序</option><option value="status">在线优先</option><option value="name">名称</option><option value="cpu">CPU</option><option value="mem">内存</option><option value="disk">磁盘</option><option value="down">下载</option><option value="up">上传</option></select><button class="btn primary" type="button" onclick="openPing()">管理探测</button></div>
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
function bytes(v){v=Number(v)||0;if(v<=0)return'0 B';const u=['B','KiB','MiB','GiB','TiB','PiB'];const i=Math.min(Math.floor(Math.log(v)/Math.log(1024)),u.length-1);return(v/Math.pow(1024,i)).toFixed(i?2:0)+' '+u[i]}
function speed(v){v=Number(v)||0;if(v<=0)return'0 B/s';const u=['B/s','KiB/s','MiB/s','GiB/s'];const i=Math.min(Math.floor(Math.log(v)/Math.log(1024)),u.length-1);return(v/Math.pow(1024,i)).toFixed(i?2:0)+' '+u[i]}
function pct(v){return Math.max(0,Math.min(100,Number(v)||0))}
function color(v){v=pct(v);return v<65?'var(--green)':v<85?'var(--yellow)':'var(--red)'}
function uptime(s){s=Number(s)||0;const d=Math.floor(s/86400);if(d)return d+'天';const h=Math.floor(s/3600);if(h)return h+'小时';return Math.floor(s/60)+'分钟'}
function ago(ts){const s=Math.max(0,Math.floor(Date.now()/1000-(Number(ts)||0)));if(s<60)return s+'秒前';if(s<3600)return Math.floor(s/60)+'分钟前';if(s<86400)return Math.floor(s/3600)+'小时前';return Math.floor(s/86400)+'天前'}
function mainDisk(disks){return Array.isArray(disks)&&disks.length?(disks.find(d=>d.mount==='/')||disks[0]):{usage_percent:0,used:0,total:0,mount:'/'}}
function netTotals(d){let rx=0,tx=0,rs=0,ts=0;for(const n of (d.network||[])){rx+=Number(n.rx_bytes)||0;tx+=Number(n.tx_bytes)||0;const s=(d.network_speeds||{})[n.interface]||{};rs+=Number(s.rx_speed)||0;ts+=Number(s.tx_speed)||0}return{rx,tx,rs,ts}}
function labelOf(s){return s.meta?.display_name||s.data?.name||s.name||'unknown'}
function dayText(ts){return new Date(ts*1000).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})}
function rail(st,isOnline){const days=Array.isArray(st?.days)?st.days:[];const pctv=st?.days_percent??st?.percent;const cells=(days.length?days:Array.from({length:90},(_,i)=>({ts:Math.floor(Date.now()/86400000)*86400-(89-i)*86400,total:0,ratio:null}))).map(d=>{const cls=!d.total?(isOnline?'uptime-seg empty':'uptime-seg bad empty'):(d.ratio>=99?'uptime-seg':'uptime-seg bad');const t=d.total?`${dayText(d.ts)} ${d.ratio.toFixed(2)}%`:`${dayText(d.ts)} 无采样`;return`<i class="${cls}" title="${t}"></i>`}).join('');return`<div class="stability"><div><b>${pctv==null?'--':pctv.toFixed(2)+'%'}</b><br><span>近90天稳定性</span></div><div class="rail">${cells}</div></div>`}
function pingHtml(s){const rows=s.ping?.last||[];if(!rows.length)return'<div class="ping"><span class="ping-pill">未配置探测</span></div>';return'<div class="ping">'+rows.slice(0,3).map(r=>`<span class="ping-pill ${r.ok?'ok':'bad'}">${esc(r.task_name||r.target||'ping')} ${r.ok?(Number(r.latency_ms)||0).toFixed(0)+'ms':'失败'}</span>`).join('')+'</div>'}
function renderSummary(list){const visible=list.filter(s=>!s.meta?.hidden),online=visible.filter(s=>s.online).length,total=visible.length,offline=total-online;let rx=0,tx=0,rs=0,ts=0;for(const s of visible){const n=netTotals(s.data||{});rx+=n.rx;tx+=n.tx;rs+=n.rs;ts+=n.ts}document.getElementById('headline').textContent=total?Math.round(online/total*100)+'%':'--';document.getElementById('headline-sub').textContent=`${online} 在线 / ${offline} 离线，上传 ${speed(ts)}，下载 ${speed(rs)}`;document.getElementById('stats').innerHTML=`<div class="stat"><b>${total}</b><span>节点</span></div><div class="stat"><b>${online}</b><span>在线</span></div><div class="stat"><b>${offline}</b><span>离线</span></div><div class="stat"><b>${bytes(tx)}</b><span>总上传</span></div>`;document.getElementById('fleet-sub').textContent=total+' nodes';document.getElementById('fleet-strip').innerHTML=(visible.length?visible:list).map(s=>`<span class="fleet-seg ${s.online?'':'off'} ${s.meta?.hidden?'hidden':''}" title="${esc(labelOf(s))}"></span>`).join('')}
function renderFilters(list){const groups=new Set();for(const s of list){if(s.meta?.group)groups.add(s.meta.group);for(const t of (s.data?.tags||[]))groups.add(t)}const base=[['all','全部'],['online','在线'],['offline','离线'],['hidden','隐藏']];let html=base.map(([id,label])=>`<button class="chip ${activeFilter===id?'active':''}" data-filter="${id}" type="button">${label}</button>`).join('');for(const g of [...groups].sort())html+=`<button class="chip ${activeFilter===g?'active':''}" data-filter="${attr(g)}" type="button">${esc(g)}</button>`;document.getElementById('filters').innerHTML=html}
function card(s){const d=s.data||{},m=s.meta||{},disk=mainDisk(d.disk),net=netTotals(d),cpu=pct(d.cpu?.usage),mem=pct(d.memory?.usage_percent),du=pct(disk.usage_percent),tags=[m.group,...(d.tags||[])].filter(Boolean).slice(0,5);return`<article class="card ${s.online?'':'offline'}"><div class="actions"><button class="icon" data-action="edit" data-name="${attr(s.name)}" title="编辑">✎</button><button class="icon" data-action="delete" data-name="${attr(s.name)}" title="删除">×</button></div><div class="identity"><span class="dot"></span><div><div class="node-name" title="${attr(labelOf(s))}">${esc(labelOf(s))}</div><div class="node-real">${esc(d.name||s.name)} · ${s.online?'刚刚更新':'离线 '+ago(s.last_seen)}</div></div><div class="note">${esc(m.note||d.os?.distro||'')}</div><div class="chips">${tags.map((t,i)=>`<span class="tag ${i===0&&m.group?'group':''}">${esc(t)}</span>`).join('')}</div></div><div><div class="metrics">${[['CPU',cpu,cpu.toFixed(2)+'%'],['内存',mem,mem.toFixed(2)+'%'],['磁盘',du,du.toFixed(2)+'%'],['上传',0,speed(net.ts)],['下载',0,speed(net.rs)]].map(x=>`<div class="metric"><label>${x[0]}</label><strong>${x[2]}</strong>${x[1]?`<div class="meter"><i style="width:${x[1]}%;background:${color(x[1])}"></i></div>`:''}</div>`).join('')}</div><div class="traffic"><div>上传 ${bytes(net.tx)}</div><div>下载 ${bytes(net.rx)}</div></div>${pingHtml(s)}</div>${rail(s.stability,s.online)}</article>`}
function refreshView(){const list=Object.values(allData);renderSummary(list);renderFilters(list);let rows=list.filter(s=>activeFilter==='hidden'?s.meta?.hidden:!s.meta?.hidden);if(activeFilter==='online')rows=rows.filter(s=>s.online);else if(activeFilter==='offline')rows=rows.filter(s=>!s.online);else if(!['all','hidden'].includes(activeFilter))rows=rows.filter(s=>s.meta?.group===activeFilter||(s.data?.tags||[]).includes(activeFilter));const sort=document.getElementById('sort-sel').value;rows=[...rows].sort((a,b)=>{const ad=a.data||{},bd=b.data||{};if(sort==='status')return Number(b.online)-Number(a.online);if(sort==='cpu')return (bd.cpu?.usage||0)-(ad.cpu?.usage||0);if(sort==='mem')return (bd.memory?.usage_percent||0)-(ad.memory?.usage_percent||0);if(sort==='disk')return mainDisk(bd.disk).usage_percent-mainDisk(ad.disk).usage_percent;if(sort==='down')return netTotals(bd).rs-netTotals(ad).rs;if(sort==='up')return netTotals(bd).ts-netTotals(ad).ts;if(sort==='name')return labelOf(a).localeCompare(labelOf(b),'zh-CN');return (b.meta?.weight||0)-(a.meta?.weight||0)||labelOf(a).localeCompare(labelOf(b),'zh-CN')});document.getElementById('servers').innerHTML=rows.length?rows.map(card).join(''):'<div class="empty">暂无匹配节点</div>'}
async function go(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});try{allData=await jsonFetch('/api/servers');refreshView()}catch(e){console.error(e)}}
function closeModal(){document.getElementById('modal').classList.remove('open')}
function showModal(html){document.getElementById('dialog').innerHTML=html;document.getElementById('modal').classList.add('open')}
function openMeta(name){const s=allData[name],m=s?.meta||{};showModal(`<div class="dialog-head"><h2>编辑节点</h2><button class="icon" onclick="closeModal()">×</button></div><div class="form-grid"><input id="meta-name" type="hidden" value="${attr(name)}"><div class="field"><label>展示名称</label><input id="meta-display" value="${attr(m.display_name||'')}"></div><div class="field"><label>分组</label><input id="meta-group" value="${attr(m.group||'')}"></div><div class="field"><label>排序权重</label><input id="meta-weight" type="number" value="${Number(m.weight)||0}"></div><div class="field"><label>隐藏</label><select id="meta-hidden"><option value="false" ${m.hidden?'':'selected'}>显示</option><option value="true" ${m.hidden?'selected':''}>隐藏</option></select></div><div class="field full"><label>备注</label><textarea id="meta-note">${esc(m.note||'')}</textarea></div></div><p class="row"><button class="btn primary" onclick="saveMeta()">保存更改</button><button class="btn" onclick="closeModal()">取消</button></p>`)}
async function saveMeta(){const name=document.getElementById('meta-name').value;await jsonFetch('/api/server/meta',{method:'POST',body:JSON.stringify({name,display_name:document.getElementById('meta-display').value,group:document.getElementById('meta-group').value,note:document.getElementById('meta-note').value,weight:Number(document.getElementById('meta-weight').value)||0,hidden:document.getElementById('meta-hidden').value==='true'})});closeModal();await go()}
async function openPing(){showModal(`<div class="dialog-head"><h2>探测任务</h2><button class="icon" onclick="closeModal()">×</button></div><div class="form-grid"><input id="task-id" type="hidden"><div class="field"><label>名称</label><input id="task-name" placeholder="API 延迟"></div><div class="field"><label>类型</label><select id="task-type"><option value="tcp">TCP</option><option value="http">HTTP</option><option value="icmp">ICMP</option></select></div><div class="field full"><label>目标</label><input id="task-target" placeholder="example.com:443 或 https://example.com"></div><div class="field"><label>执行节点</label><select id="task-server"><option value="">全部节点</option>${Object.values(allData).map(s=>`<option value="${attr(s.name)}">${esc(labelOf(s))}</option>`).join('')}</select></div><div class="field"><label>间隔/秒</label><input id="task-interval" type="number" min="10" value="60"></div><div class="field"><label>状态</label><select id="task-enabled"><option value="true">启用</option><option value="false">停用</option></select></div></div><p class="row"><button class="btn primary" onclick="savePingTask()">保存任务</button><button class="btn" onclick="resetPingForm()">新建</button></p><div class="task-list" id="task-list">加载中...</div>`);await loadPingTasks()}
async function loadPingTasks(){const j=await jsonFetch('/api/ping-tasks');pingTasks=j.tasks||[];document.getElementById('task-list').innerHTML=pingTasks.length?pingTasks.map(t=>`<div class="task"><div><b>${esc(t.name||t.target)}</b><small>${esc(t.type)} · ${esc(t.target)} · ${t.server?esc(t.server):'全部节点'} · ${t.interval}s · ${t.enabled?'启用':'停用'}</small></div><div class="row"><button class="btn" onclick="editPingTask('${attr(t.id)}')">编辑</button><button class="btn" onclick="deletePingTask('${attr(t.id)}')">删除</button></div></div>`).join(''):'<div class="empty">还没有探测任务</div>'}
function resetPingForm(){for(const id of ['task-id','task-name','task-target'])document.getElementById(id).value='';document.getElementById('task-type').value='tcp';document.getElementById('task-server').value='';document.getElementById('task-interval').value=60;document.getElementById('task-enabled').value='true'}
function editPingTask(id){const t=pingTasks.find(x=>x.id===id);if(!t)return;document.getElementById('task-id').value=t.id;document.getElementById('task-name').value=t.name||'';document.getElementById('task-type').value=t.type||'tcp';document.getElementById('task-target').value=t.target||'';document.getElementById('task-server').value=t.server||'';document.getElementById('task-interval').value=t.interval||60;document.getElementById('task-enabled').value=String(t.enabled!==false)}
async function savePingTask(){await jsonFetch('/api/ping-task',{method:'POST',body:JSON.stringify({id:document.getElementById('task-id').value,name:document.getElementById('task-name').value,type:document.getElementById('task-type').value,target:document.getElementById('task-target').value,server:document.getElementById('task-server').value,interval:Number(document.getElementById('task-interval').value)||60,enabled:document.getElementById('task-enabled').value==='true'})});resetPingForm();await loadPingTasks()}
async function deletePingTask(id){await jsonFetch('/api/ping-task?id='+encodeURIComponent(id),{method:'DELETE'});await loadPingTasks()}
async function deleteServer(name,btn){if(!btn.dataset.confirm){btn.dataset.confirm='1';btn.textContent='!';setTimeout(()=>{btn.dataset.confirm='';btn.textContent='×'},2200);return}await jsonFetch('/api/server?name='+encodeURIComponent(name),{method:'DELETE'});await go()}
function toggleTheme(){const dark=document.documentElement.getAttribute('data-theme')==='dark';if(dark){document.documentElement.removeAttribute('data-theme');localStorage.setItem('probe-theme','light')}else{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('probe-theme','dark')}}
document.getElementById('filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;activeFilter=b.dataset.filter;refreshView()});
document.getElementById('servers').addEventListener('click',e=>{const b=e.target.closest('[data-action]');if(!b)return;if(b.dataset.action==='edit')openMeta(b.dataset.name);if(b.dataset.action==='delete')deleteServer(b.dataset.name,b)});
document.getElementById('modal').addEventListener('click',e=>{if(e.target.id==='modal')closeModal()});
if(localStorage.getItem('probe-theme')==='dark')document.documentElement.setAttribute('data-theme','dark');document.getElementById('ri').textContent=R;if(AUTH_REQUIRED&&!TOKEN){setTokenPrompt()}else{go();startRefresh()}
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
