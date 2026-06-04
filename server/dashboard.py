#!/usr/bin/env python3
"""
⚡ ServerProbe Dashboard - 集中监控面板
接收多个VPS Agent上报的数据，在统一页面展示
内存占用 ~5-10MB，CPU <1%
"""

import http.server
import json
import os
import time
import threading
import socketserver
from urllib.parse import urlparse, parse_qs

# ============ 配置 ============
PORT = int(os.environ.get('PORT', '8080'))
OFFLINE_TIMEOUT = int(os.environ.get('OFFLINE_TIMEOUT', '15'))
AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '')  # 留空则不鉴权
PERSIST_FILE = os.environ.get('PERSIST_FILE', '')  # 留空则不持久化，如 /opt/server-probe/data.json
PERSIST_INTERVAL = int(os.environ.get('PERSIST_INTERVAL', '60'))  # 持久化间隔秒
MAX_BODY_SIZE = 10240  # 10KB，防OOM
GHOST_TIMEOUT_DAYS = int(os.environ.get('GHOST_TIMEOUT_DAYS', '30'))  # 超过N天未上报自动清理幽灵条目，0=不清理
STABILITY_SAMPLE_INTERVAL = int(os.environ.get('STABILITY_SAMPLE_INTERVAL', '60'))  # 稳定性采样间隔秒
STABILITY_RETENTION_DAYS = int(os.environ.get('STABILITY_RETENTION_DAYS', '90'))  # 稳定性保留天数
STABILITY_DISPLAY_DAYS = int(os.environ.get('STABILITY_DISPLAY_DAYS', '7'))  # 页面默认展示最近N天小时格
# ==============================

servers = {}
stability = {}
lock = threading.Lock()

def hour_bucket(ts=None):
    """返回所在小时的 epoch 秒。"""
    ts = time.time() if ts is None else ts
    return int(ts // 3600 * 3600)

def normalize_bucket(bucket):
    """兼容旧/脏数据，保证 bucket 可安全计算。"""
    if not isinstance(bucket, dict):
        return {'ok': 0, 'total': 0}
    ok = int(bucket.get('ok', 0) or 0)
    total = int(bucket.get('total', 0) or 0)
    return {'ok': max(0, ok), 'total': max(0, total)}

def prune_stability(now=None):
    """裁剪超过保留周期的小时桶。调用方需持有 lock。"""
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
    """按当前在线状态写入稳定性小时桶。"""
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
    """生成前端需要的最近N天小时格和总体稳定性。调用方需持有 lock。"""
    now = time.time() if now is None else now
    end = hour_bucket(now)
    display_days = max(1, min(STABILITY_DISPLAY_DAYS, STABILITY_RETENTION_DAYS))
    display_hours = display_days * 24
    start = end - (display_hours - 1) * 3600
    buckets = stability.get(name, {})
    hours = []
    ok_sum = total_sum = 0
    for ts in range(start, end + 1, 3600):
        b = normalize_bucket(buckets.get(str(ts), {}))
        ok = b['ok']
        total = b['total']
        ratio = round(ok / total * 100, 2) if total else None
        if total:
            ok_sum += ok
            total_sum += total
        hours.append({'ts': ts, 'ok': ok, 'total': total, 'ratio': ratio})
    percent = round(ok_sum / total_sum * 100, 2) if total_sum else None
    return {
        'percent': percent,
        'hours': hours,
        'display_days': display_days,
        'retention_days': STABILITY_RETENTION_DAYS,
        'sample_interval': STABILITY_SAMPLE_INTERVAL,
    }

def load_persist():
    """从文件加载持久化数据"""
    if not PERSIST_FILE:
        return
    try:
        with open(PERSIST_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('version') == 2 and isinstance(data.get('servers'), dict):
            server_data = data.get('servers', {})
            stability_data = data.get('stability', {})
        else:
            # 兼容旧版持久化：文件根对象就是 servers 字典
            server_data = data
            stability_data = {}
        with lock:
            for name, srv in server_data.items():
                # 重置 last_seen 为 0，避免重启后短暂误判为在线
                srv['last_seen'] = 0
                servers[name] = srv
            for name, buckets in stability_data.items():
                if isinstance(buckets, dict):
                    stability[name] = {str(k): normalize_bucket(v) for k, v in buckets.items()}
            prune_stability()
        print(f'📂 已加载持久化数据: {len(server_data)} 台服务器，{len(stability)} 份稳定性记录 (last_seen 已重置)')
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f'⚠️ 加载持久化数据失败: {e}')

def persist_loop():
    """定期持久化到文件"""
    if not PERSIST_FILE:
        return
    while True:
        time.sleep(PERSIST_INTERVAL)
        try:
            with lock:
                data = {
                    'version': 2,
                    'servers': dict(servers),
                    'stability': {name: {k: dict(v) for k, v in buckets.items()} for name, buckets in stability.items()},
                }
            tmp = PERSIST_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, PERSIST_FILE)  # 原子写入
        except Exception as e:
            print(f'⚠️ 持久化失败: {e}')

def cleanup_ghosts():
    """后台清理幽灵条目（超过GHOST_TIMEOUT_DAYS天未上报的旧机器）"""
    if GHOST_TIMEOUT_DAYS <= 0:
        return
    threshold = GHOST_TIMEOUT_DAYS * 86400
    while True:
        now = time.time()
        with lock:
            ghosts = [k for k, v in servers.items()
                      if v['last_seen'] > 0 and now - v['last_seen'] > threshold]
            for k in ghosts:
                del servers[k]
                stability.pop(k, None)
            prune_stability(now)
        if ghosts:
            print(f'👻 清理 {len(ghosts)} 个幽灵条目 (超过{GHOST_TIMEOUT_DAYS}天未上报): {ghosts}')
        time.sleep(3600)  # 每小时检查一次

def stability_loop():
    """定时采样每台服务器的在线状态，用于生成小时级稳定性热力图。"""
    if STABILITY_SAMPLE_INTERVAL <= 0:
        return
    while True:
        record_stability_sample()
        time.sleep(STABILITY_SAMPLE_INTERVAL)

# ============ 前端页面 ============

HTML_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小鸡儿 · 我的VPS监控</title>
<meta name="probe-token" content="__AUTH_TOKEN__">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  color-scheme:light;
  --bg:#f7f7f4;--panel:#fff;--panel-soft:#fbfbf9;--text:#1f2024;--muted:#8d8e91;--sub:#5b5d63;
  --line:#e9e9e4;--line-strong:#ddddd5;--shadow:0 10px 28px rgba(24,24,24,.06);
  --green:#22c879;--red:#ef335b;--blue:#3775f6;--yellow:#f0a626;--purple:#9350e8;--pink:#e044a7;--dark:#23201d;
  --radius:17px;--radius-sm:10px;--card-h:206px;
}
[data-theme="dark"]{
  color-scheme:dark;
  --bg:#111214;--panel:#1a1c20;--panel-soft:#15171a;--text:#f1f3f4;--muted:#8c929a;--sub:#bdc3ca;
  --line:#2a2d31;--line-strong:#3a3f45;--shadow:0 14px 32px rgba(0,0,0,.34);
}
body{min-height:100vh;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Arial,sans-serif;line-height:1.45;letter-spacing:0}
button,select{font:inherit;color:inherit}button{cursor:pointer}.wrap{width:min(1280px,calc(100vw - 44px));margin:0 auto;padding:22px 0 30px}
.topbar{height:50px;display:flex;align-items:center;justify-content:space-between;margin-bottom:58px}.brand{display:flex;align-items:center;gap:10px;min-width:0}.logo{width:27px;height:27px;border-radius:5px;background:#050505;color:#fff;display:grid;place-items:center;font-size:18px;font-weight:900;line-height:1;box-shadow:0 2px 8px rgba(0,0,0,.16)}.brand-name{font-size:18px;font-weight:760;white-space:nowrap}.brand-sub{font-size:14px;color:var(--muted);padding-left:4px;border-left:1px solid var(--line);white-space:nowrap}.top-actions{display:flex;align-items:center;gap:12px}.admin-link{font-size:14px;color:var(--muted);text-decoration:none;white-space:nowrap}.icon-btn{width:39px;height:39px;border-radius:999px;border:1px solid var(--line);background:var(--panel);display:grid;place-items:center;box-shadow:0 1px 5px rgba(0,0,0,.03);transition:border-color .16s ease,transform .16s ease}.icon-btn:hover{border-color:var(--line-strong);transform:translateY(-1px)}.online-pill{height:39px;border:1px solid var(--line);background:var(--panel);border-radius:999px;display:flex;align-items:center;gap:10px;padding:0 16px;font-weight:760;box-shadow:0 1px 5px rgba(0,0,0,.03)}.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(34,200,121,.12)}
.hero{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:38px;gap:24px}.hero-title{font-size:18px;font-weight:820;display:flex;align-items:center;gap:9px}.hero-time{font-size:16px;color:var(--muted);margin-top:2px}.hero-time strong{color:var(--sub);font-weight:650}.hero-art{position:relative;width:142px;height:112px;flex:0 0 auto;margin-right:26px}.art-head{position:absolute;right:36px;top:0;width:45px;height:45px;background:#050505;border-radius:50%;box-shadow:inset 13px 0 0 #171717}.art-body{position:absolute;right:27px;top:38px;width:58px;height:49px;border:5px solid #111;border-top:0;border-radius:0 0 23px 23px;transform:rotate(-6deg)}.art-arm{position:absolute;right:76px;top:55px;width:45px;height:30px;border-bottom:4px solid #111;transform:rotate(20deg)}.art-leg{position:absolute;right:18px;bottom:9px;width:80px;height:42px;border-bottom:5px solid #111;border-left:5px solid #111;border-radius:0 0 0 18px;transform:rotate(13deg)}.art-book{position:absolute;right:76px;top:61px;width:54px;height:27px;border:3px solid #111;border-radius:3px;transform:rotate(18deg);background:var(--bg)}
.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px;margin-bottom:34px}.summary-card{height:116px;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:23px 28px;position:relative;overflow:hidden}.summary-label{font-size:17px;font-weight:690;color:var(--sub)}.summary-value{font-size:24px;font-weight:820;margin-top:7px;display:flex;align-items:center;gap:11px;font-variant-numeric:tabular-nums}.summary-value i{width:8px;height:8px;border-radius:50%;display:inline-block}.summary-hint{font-size:12px;color:#59618d;margin-top:5px;white-space:nowrap}.net-card{padding-right:100px}.net-lines{font-size:12px;color:var(--sub);margin-top:8px;display:flex;gap:10px;flex-wrap:wrap}.net-orn{position:absolute;right:20px;bottom:16px;width:58px;height:58px;border-radius:16px;background:linear-gradient(145deg,#f4f4f0,#fff);border:1px solid var(--line);display:grid;place-items:center;font-weight:900;font-size:21px;color:#111}
.toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;gap:16px}.filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.view-btn,.chip,.sort-btn{border:1px solid var(--line);background:var(--panel);border-radius:999px;min-height:41px;box-shadow:0 2px 9px rgba(0,0,0,.03)}.view-btn{width:41px;display:grid;place-items:center;font-weight:800}.chip{padding:0 14px;display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-weight:690;transition:color .16s ease,border-color .16s ease,box-shadow .16s ease}.chip:hover{border-color:var(--line-strong);color:var(--text)}.chip.active{background:var(--panel);color:var(--text);border-color:#ededed;box-shadow:0 7px 18px rgba(0,0,0,.08)}.chip .pin{width:7px;height:7px;border-radius:50%;background:var(--blue);box-shadow:0 0 0 4px rgba(55,117,246,.08)}.sort-wrap{position:relative}.sort-btn{appearance:none;padding:0 38px 0 16px;font-weight:760;cursor:pointer;outline:none}.sort-wrap:after{content:"↕";position:absolute;right:14px;top:9px;color:var(--muted);font-weight:800;pointer-events:none}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 14px}.server-card{height:var(--card-h);background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 5px 18px rgba(0,0,0,.04);padding:14px 18px 13px 18px;display:grid;grid-template-columns:200px 1fr;gap:18px;position:relative;overflow:hidden;transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}.server-card:hover{transform:translateY(-1px);border-color:var(--line-strong);box-shadow:0 10px 26px rgba(0,0,0,.07)}.server-card.offline{opacity:.62}.identity{display:grid;grid-template-columns:15px 28px 1fr;align-items:center;column-gap:9px;min-width:0;align-self:center}.status{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(34,200,121,.1)}.server-card.offline .status{background:var(--red);box-shadow:0 0 0 5px rgba(239,51,91,.1)}.flag{font-size:20px;filter:saturate(.95)}.name{font-size:14px;font-weight:850;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.subline{grid-column:3;font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}.uptime-line{grid-column:3;font-size:11px;color:var(--muted);margin-top:1px;white-space:nowrap}.mini-bar{grid-column:3;height:3px;width:84px;background:#efefeb;border-radius:999px;overflow:hidden;margin-top:5px}.mini-bar span{display:block;height:100%;background:linear-gradient(90deg,var(--red),var(--yellow),var(--green));border-radius:999px}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;align-items:start}.metric-title{font-size:12px;color:var(--muted);font-weight:700}.metric-value{font-size:14px;font-weight:850;margin-top:1px;white-space:nowrap;font-variant-numeric:tabular-nums}.meter{height:3px;background:#ececea;border-radius:999px;margin-top:5px;overflow:hidden}.meter span{display:block;height:100%;border-radius:999px}.traffic{position:absolute;left:236px;right:18px;bottom:82px;display:grid;grid-template-columns:1fr 1fr;gap:8px}.traffic-box{height:28px;background:var(--panel-soft);border:1px solid var(--line);border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:760;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.badges{position:absolute;left:236px;right:52px;bottom:54px;display:flex;align-items:center;gap:5px;overflow:hidden}.tag{font-size:10px;line-height:18px;height:18px;padding:0 7px;border-radius:5px;color:#fff;font-weight:800;white-space:nowrap}.b-blue{background:#2d7df0}.b-green{background:#23bf75}.b-purple{background:#914bdb}.b-pink{background:#d933a3}.b-dark{background:#57524a}.delete-btn{position:absolute;right:15px;bottom:11px;width:26px;height:26px;border:1px solid transparent;background:transparent;color:#c0c0bd;border-radius:50%;font-size:18px;line-height:1}.delete-btn:hover,.delete-btn.confirm{border-color:#f0c8d0;color:var(--red);background:#fff3f5}.empty{grid-column:1/-1;height:260px;border:1px dashed var(--line-strong);border-radius:var(--radius);display:grid;place-items:center;color:var(--muted);font-size:15px;background:rgba(255,255,255,.35)}.footer{text-align:center;color:var(--muted);font-size:11px;padding:28px 0 4px}
.stability-wrap{position:absolute;left:18px;right:52px;bottom:12px;display:grid;grid-template-columns:132px minmax(0,1fr);align-items:center;gap:12px;min-width:0}.stability-text{font-size:11px;color:var(--muted);line-height:1.1;white-space:nowrap}.stability-text strong{display:inline-block;color:var(--text);font-size:13px;font-weight:850;font-variant-numeric:tabular-nums;margin-right:6px}.stability-grid{display:grid;grid-auto-rows:14px;gap:3px;align-items:center;min-width:0;overflow:hidden}.stability-cell{height:14px;min-width:2px;border-radius:999px;background:#e9e9e4}.u-online{background:#16bd78}.u-offline{background:#ef4f58}.u-empty{opacity:.38}
@media(max-width:1120px){.server-card{grid-template-columns:178px 1fr}.traffic,.badges{left:214px}.metric-value{font-size:13px}}
@media(max-width:980px){.summary{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{grid-template-columns:1fr}.hero-art{display:none}.topbar{margin-bottom:34px}.server-card{grid-template-columns:192px 1fr}.traffic,.badges{left:228px}}
@media(max-width:640px){.wrap{width:min(100vw - 24px,1280px);padding-top:12px}.brand-sub,.admin-link{display:none}.topbar{margin-bottom:26px}.top-actions{gap:8px}.icon-btn{width:36px;height:36px}.online-pill{height:36px;padding:0 12px}.hero{margin-bottom:20px}.summary{grid-template-columns:1fr;gap:10px}.summary-card{height:96px;padding:18px}.toolbar{align-items:stretch;flex-direction:column}.sort-btn{width:100%}.grid{gap:10px}.server-card{height:auto;min-height:246px;grid-template-columns:1fr;padding:14px}.identity{padding-right:28px}.metrics{grid-template-columns:repeat(3,1fr)}.traffic{position:static;grid-column:1;margin-top:8px}.badges{position:static;grid-column:1;margin-top:2px;flex-wrap:wrap}.stability-wrap{position:static;grid-column:1;margin-top:10px;grid-template-columns:1fr;gap:7px}.delete-btn{top:10px;right:10px;bottom:auto}}
</style>
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <div class="brand">
      <div class="logo">N</div>
      <div class="brand-name">小鸡儿</div>
      <div class="brand-sub">我的VPS监控</div>
    </div>
    <div class="top-actions">
      <a class="admin-link" href="/">管理后台</a>
      <button class="icon-btn" type="button" title="语言">文</button>
      <button class="icon-btn" type="button" title="切换主题" onclick="toggleTheme()">☼</button>
      <div class="online-pill"><span id="top-online">0 在线</span><span class="live-dot"></span></div>
    </div>
  </header>

  <section class="hero">
    <div>
      <div class="hero-title">👋 概览</div>
      <div class="hero-time">当前时间 <strong id="clock">--:--:--</strong></div>
    </div>
    <div class="hero-art" aria-hidden="true">
      <div class="art-head"></div><div class="art-body"></div><div class="art-arm"></div><div class="art-leg"></div><div class="art-book"></div>
    </div>
  </section>

  <section class="summary" id="summary"></section>

  <section class="toolbar">
    <div class="filters" id="filters">
      <button class="view-btn" type="button" title="卡片视图">▣</button>
      <button class="view-btn" type="button" title="紧凑视图">▤</button>
      <button class="view-btn" type="button" title="统计视图">▥</button>
    </div>
    <label class="sort-wrap" title="排序">
      <select class="sort-btn" id="sort-sel" onchange="refreshView()">
        <option value="name">Sort</option>
        <option value="status">在线优先</option>
        <option value="cpu">CPU</option>
        <option value="mem">内存</option>
        <option value="disk">存储</option>
        <option value="down">下载</option>
        <option value="up">上传</option>
      </select>
    </label>
  </section>

  <main class="grid" id="servers"></main>
  <footer class="footer">ServerProbe · <span id="ri"></span>s 刷新</footer>
</div>

<script>
const R=3;
const TOKEN=document.querySelector('meta[name="probe-token"]').content;
let allData={};
let activeFilter='all';

function apiUrl(path){if(!TOKEN)return path;const sep=path.includes('?')?'&':'?';return path+sep+'token='+encodeURIComponent(TOKEN)}
function esc(v){const d=document.createElement('div');d.textContent=String(v??'');return d.innerHTML}
function bytes(v){v=Number(v)||0;if(v<=0)return'0 B';const u=['B','KiB','MiB','GiB','TiB','PiB'];const i=Math.min(Math.floor(Math.log(v)/Math.log(1024)),u.length-1);return(v/Math.pow(1024,i)).toFixed(i?2:0)+' '+u[i]}
function speed(v){v=Number(v)||0;if(v<=0)return'0 B/s';const u=['B/s','KiB/s','MiB/s','GiB/s'];const i=Math.min(Math.floor(Math.log(v)/Math.log(1024)),u.length-1);return(v/Math.pow(1024,i)).toFixed(i?2:0)+' '+u[i]}
function pct(v){v=Number(v)||0;return Math.max(0,Math.min(100,v))}
function metricColor(v){v=pct(v);return v<60?'var(--green)':v<84?'var(--yellow)':'var(--red)'}
function uptime(s){s=Number(s)||0;const d=Math.floor(s/86400);if(d>0)return d+' 天';const h=Math.floor(s/3600);if(h>0)return h+' 小时';return Math.floor(s/60)+' 分钟'}
function ago(ts){const s=Math.max(0,Math.floor(Date.now()/1000-(Number(ts)||0)));if(s<60)return s+' 秒前';if(s<3600)return Math.floor(s/60)+' 分钟前';if(s<86400)return Math.floor(s/3600)+' 小时前';return Math.floor(s/86400)+' 天前'}
function flagFor(d){const text=((d.tags||[]).join(' ')+d.name+' '+(d.os?.distro||'')).toLowerCase();if(/hk|香港/.test(text))return'🇭🇰';if(/jp|日本|tokyo|osaka/.test(text))return'🇯🇵';if(/sg|新加坡/.test(text))return'🇸🇬';if(/kr|韩国|首尔|seoul/.test(text))return'🇰🇷';if(/us|美国|seattle|la|los|ny|dallas|ashburn/.test(text))return'🇺🇸';if(/cn|中国|广州|上海|北京|深圳/.test(text))return'🇨🇳';return'🏳️'}
function mainDisk(disks){if(!Array.isArray(disks)||!disks.length)return{usage_percent:0,used:0,total:0,mount:'/'};return disks.find(d=>d.mount==='/')||disks[0]}
function netTotals(d){const net=Array.isArray(d.network)?d.network:[];const sp=d.network_speeds||{};let rx=0,tx=0,rs=0,ts=0;for(const n of net){rx+=Number(n.rx_bytes)||0;tx+=Number(n.tx_bytes)||0;const s=sp[n.interface]||{};rs+=Number(s.rx_speed)||0;ts+=Number(s.tx_speed)||0}return{rx,tx,rs,ts}}
function uptimeClass(h,isOnline){if(!h.total)return(isOnline?'u-online':'u-offline')+' u-empty';return h.ratio>=99?'u-online':'u-offline'}
function formatHour(ts){return new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}
function stabilityGrid(st,isOnline){st=st||{};const hours=Array.isArray(st.hours)?st.hours:[];const visible=hours.length?hours:Array.from({length:24},(_,i)=>({ts:Math.floor(Date.now()/3600000)*3600-(23-i)*3600,total:0,ok:0,ratio:null}));const pct=st.percent===null||st.percent===undefined?'--':st.percent.toFixed(2)+'%';const days=st.display_days||7;const cells=visible.map(h=>{const text=h.total?`${formatHour(h.ts)} · ${h.ratio.toFixed(2)}% · ${h.ok}/${h.total}`:`${formatHour(h.ts)} · 无历史采样`;return`<span class="stability-cell ${uptimeClass(h,isOnline)}" title="${text}"></span>`}).join('');const cols=Math.max(1,visible.length);return`<div class="stability-wrap"><div class="stability-text"><strong>${pct}</strong>近${days}天稳定性</div><div class="stability-grid" style="grid-template-columns:repeat(${cols},minmax(2px,1fr))">${cells}</div></div>`}
function themeInit(){if(localStorage.getItem('probe-theme')==='dark')document.documentElement.setAttribute('data-theme','dark')}
function toggleTheme(){const dark=document.documentElement.getAttribute('data-theme')==='dark';if(dark){document.documentElement.removeAttribute('data-theme');localStorage.setItem('probe-theme','light')}else{document.documentElement.setAttribute('data-theme','dark');localStorage.setItem('probe-theme','dark')}}

function renderSummary(list){
  const total=list.length, online=list.filter(s=>s.online).length, offline=total-online;
  let rx=0,tx=0,rs=0,ts=0;for(const s of list){const n=netTotals(s.data||{});rx+=n.rx;tx+=n.tx;rs+=n.rs;ts+=n.ts}
  document.getElementById('top-online').textContent=online+' 在线';
  document.getElementById('summary').innerHTML=`
    <div class="summary-card"><div class="summary-label">服务器总数</div><div class="summary-value"><i style="background:var(--blue)"></i>${total}</div></div>
    <div class="summary-card"><div class="summary-label">在线服务器</div><div class="summary-value"><i style="background:var(--green)"></i>${online}</div></div>
    <div class="summary-card"><div class="summary-label">离线服务器</div><div class="summary-value"><i style="background:var(--red)"></i>${offline}</div></div>
    <div class="summary-card net-card"><div class="summary-label">网络</div><div class="summary-hint">${bytes(tx)} | ${bytes(rx)}</div><div class="net-lines"><span>● ${speed(ts)}</span><span>● ${speed(rs)}</span></div><div class="net-orn">N</div></div>`;
}
function renderFilters(list){
  const tags=new Set();for(const s of list){for(const t of (s.data?.tags||[]))tags.add(t)}
  const base=[['all','All','var(--dark)'],['online','在线','var(--green)'],['offline','离线','var(--red)']];
  let html='<button class="view-btn" type="button" title="卡片视图">▣</button><button class="view-btn" type="button" title="紧凑视图">▤</button><button class="view-btn" type="button" title="统计视图">▥</button>';
  for(const [id,label,color] of base){html+=`<button class="chip ${activeFilter===id?'active':''}" type="button" data-filter="${id}"><span class="pin" style="background:${color}"></span>${label}</button>`}
  for(const tag of [...tags].sort()){html+=`<button class="chip ${activeFilter===tag?'active':''}" type="button" data-filter="${esc(tag)}"><span class="pin" style="background:var(--yellow)"></span>${esc(tag)}</button>`}
  document.getElementById('filters').innerHTML=html;
}
function serverCard(s){
  const d=s.data||{};const cpu=d.cpu||{},mem=d.memory||{},load=d.load||{},os=d.os||{};const disk=mainDisk(d.disk);const n=netTotals(d);const memP=pct(mem.usage_percent);const cpuP=pct(cpu.usage);const diskP=pct(disk.usage_percent);
  const distro=os.distro||os.os||'Linux';const tags=(d.tags&&d.tags.length?d.tags:[d.virtualization,distro]).filter(Boolean).slice(0,5);
  const badgeColors=['b-blue','b-green','b-purple','b-pink','b-dark'];
  const tagHtml=tags.map((t,i)=>`<span class="tag ${badgeColors[i%badgeColors.length]}">${esc(t)}</span>`).join('');
  const stableHtml=stabilityGrid(s.stability,s.online);
  return `<article class="server-card ${s.online?'':'offline'}">
    <div class="identity">
      <span class="status"></span><span class="flag">${flagFor(d)}</span><div class="name" title="${esc(d.name)}">${esc(d.name||'unknown')}</div>
      <div class="subline">${esc(distro)} · ${esc(os.arch||'')}</div>
      <div class="uptime-line">运行 ${uptime(load.uptime)} · ${s.online?'刚刚更新':'离线 '+ago(s.last_seen||d.timestamp)}</div>
      <div class="mini-bar"><span style="width:${Math.min(100,Math.max(8,100-cpuP))}%"></span></div>
    </div>
    <div class="metrics">
      <div><div class="metric-title">CPU</div><div class="metric-value">${cpuP.toFixed(2)}%</div><div class="meter"><span style="width:${cpuP}%;background:${metricColor(cpuP)}"></span></div></div>
      <div><div class="metric-title">内存</div><div class="metric-value">${memP.toFixed(2)}%</div><div class="meter"><span style="width:${memP}%;background:${metricColor(memP)}"></span></div></div>
      <div><div class="metric-title">存储</div><div class="metric-value">${diskP.toFixed(2)}%</div><div class="meter"><span style="width:${diskP}%;background:${metricColor(diskP)}"></span></div></div>
      <div><div class="metric-title">上传</div><div class="metric-value">${speed(n.ts)}</div></div>
      <div><div class="metric-title">下载</div><div class="metric-value">${speed(n.rs)}</div></div>
    </div>
    <div class="traffic"><div class="traffic-box">上传:${bytes(n.tx)}</div><div class="traffic-box">下载:${bytes(n.rx)}</div></div>
    <div class="badges">${tagHtml}</div>
    ${stableHtml}
    <button class="delete-btn" data-name="${esc(d.name||'unknown')}" onclick="deleteServer(event,this)" title="删除">×</button>
  </article>`
}
function refreshView(){
  const list=Object.values(allData);renderSummary(list);renderFilters(list);
  let rows=list;if(activeFilter==='online')rows=rows.filter(s=>s.online);else if(activeFilter==='offline')rows=rows.filter(s=>!s.online);else if(activeFilter!=='all')rows=rows.filter(s=>(s.data?.tags||[]).includes(activeFilter));
  const sort=document.getElementById('sort-sel').value;
  rows=[...rows].sort((a,b)=>{const ad=a.data||{},bd=b.data||{};if(sort==='status')return Number(b.online)-Number(a.online);if(sort==='cpu')return (bd.cpu?.usage||0)-(ad.cpu?.usage||0);if(sort==='mem')return (bd.memory?.usage_percent||0)-(ad.memory?.usage_percent||0);if(sort==='disk')return mainDisk(bd.disk).usage_percent-mainDisk(ad.disk).usage_percent;if(sort==='down')return netTotals(bd).rs-netTotals(ad).rs;if(sort==='up')return netTotals(bd).ts-netTotals(ad).ts;return String(ad.name||'').localeCompare(String(bd.name||''),'zh-CN')});
  document.getElementById('servers').innerHTML=rows.length?rows.map(serverCard).join(''):'<div class="empty">暂无服务器，启动 Agent 后会显示在这里</div>';
}
async function go(){
  document.getElementById('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  try{const r=await fetch(apiUrl('/api/servers'));allData=await r.json();refreshView()}catch(e){console.error(e)}
}
function deleteServer(e,btn){
  e.stopPropagation();const name=btn.dataset.name;if(!btn.classList.contains('confirm')){btn.classList.add('confirm');btn.textContent='!';setTimeout(()=>{btn.classList.remove('confirm');btn.textContent='×'},2400);return}
  btn.disabled=true;fetch(apiUrl('/api/server?name='+encodeURIComponent(name)),{method:'DELETE'}).then(r=>r.json()).then(j=>{if(j.status==='ok')go();else throw new Error(j.message||'删除失败')}).catch(err=>{alert('删除失败: '+err.message);btn.disabled=false;btn.textContent='×';btn.classList.remove('confirm')})
}
document.getElementById('filters').addEventListener('click',e=>{const b=e.target.closest('[data-filter]');if(!b)return;activeFilter=b.dataset.filter;refreshView()});
document.getElementById('ri').textContent=R;themeInit();go();setInterval(go,R*1000);
</script>
</body>
</html>'''
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            html = HTML_PAGE.replace('__AUTH_TOKEN__', AUTH_TOKEN or '')
            self.wfile.write(html.encode('utf-8'))
        elif path == '/api/servers':
            # GET 鉴权：支持 query param token（方便浏览器访问）
            if AUTH_TOKEN:
                token = parse_qs(urlparse(self.path).query).get('token', [''])[0]
                if token != AUTH_TOKEN:
                    self._send_json({'error': 'unauthorized'}, 403)
                    return
            now = time.time()
            result = {}
            with lock:
                for name, srv in servers.items():
                    result[name] = {
                        'data': srv['data'],
                        'online': now - srv['last_seen'] < OFFLINE_TIMEOUT,
                        'last_seen': srv['last_seen'],
                        'stability': stability_payload(name, now),
                    }
            self._send_json(result)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/report':
            # 鉴权
            if AUTH_TOKEN:
                auth = self.headers.get('Authorization', '')
                expected = f'Bearer {AUTH_TOKEN}'
                if auth != expected:
                    self._send_json({'status': 'error', 'message': 'unauthorized'}, 403)
                    return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length <= 0 or length > MAX_BODY_SIZE:
                    self._send_json({'status': 'error', 'message': f'body size must be 1-{MAX_BODY_SIZE} bytes'}, 413)
                    return
                body = self.rfile.read(length)
                if not body:
                    self._send_json({'status': 'error', 'message': 'empty body'}, 400)
                    return
                data = json.loads(body)
                name = data.get('name', 'unknown')
                with lock:
                    servers[name] = {'data': data, 'last_seen': time.time()}
                self._send_json({'status': 'ok'})
            except json.JSONDecodeError as e:
                self._send_json({'status': 'error', 'message': f'invalid json: {e}'}, 400)
            except Exception as e:
                self._send_json({'status': 'error', 'message': str(e)}, 400)
        else:
            self.send_response(404); self.end_headers()

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == '/api/server':
            # 鉴权：支持 Bearer header 或 query param token
            if AUTH_TOKEN:
                auth = self.headers.get('Authorization', '')
                expected = f'Bearer {AUTH_TOKEN}'
                token = parse_qs(urlparse(self.path).query).get('token', [''])[0]
                if auth != expected and token != AUTH_TOKEN:
                    self._send_json({'status': 'error', 'message': 'unauthorized'}, 403)
                    return
            qs = parse_qs(urlparse(self.path).query)
            name = qs.get('name', [None])[0]
            if not name:
                self._send_json({'status': 'error', 'message': 'missing name parameter'}, 400)
                return
            with lock:
                if name in servers:
                    del servers[name]
                    stability.pop(name, None)
                    self._send_json({'status': 'ok', 'message': f'server {name} deleted'})
                else:
                    self._send_json({'status': 'error', 'message': f'server {name} not found'}, 404)
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """支持并发的 HTTP Server"""
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    # 加载持久化数据
    load_persist()

    # 启动后台线程
    threading.Thread(target=cleanup_ghosts, daemon=True).start()
    threading.Thread(target=stability_loop, daemon=True).start()
    if PERSIST_FILE:
        threading.Thread(target=persist_loop, daemon=True).start()

    print(f'''
⚡ ServerProbe Dashboard 启动
   地址: http://0.0.0.0:{PORT}
   离线判定: {OFFLINE_TIMEOUT}秒
   幽灵清理: {f"{GHOST_TIMEOUT_DAYS}天" if GHOST_TIMEOUT_DAYS > 0 else "关闭"}
   稳定性采样: {f"{STABILITY_SAMPLE_INTERVAL}秒 / 保留{STABILITY_RETENTION_DAYS}天" if STABILITY_SAMPLE_INTERVAL > 0 else "关闭"}
   鉴权: {"已启用 ✓" if AUTH_TOKEN else "未启用"}
   持久化: {PERSIST_FILE or "未启用"}

📡 Agent 部署命令:
   DASHBOARD_URL=http://你的IP:{PORT} python3 agent.py
''')
    srv = ThreadedHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n🛑 已停止')
        srv.server_close()
