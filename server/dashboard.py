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
# ==============================

servers = {}
lock = threading.Lock()

def load_persist():
    """从文件加载持久化数据"""
    if not PERSIST_FILE:
        return
    try:
        with open(PERSIST_FILE, 'r') as f:
            data = json.load(f)
        with lock:
            servers.update(data)
        print(f'📂 已加载持久化数据: {len(data)} 台服务器')
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
                data = dict(servers)
            tmp = PERSIST_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, PERSIST_FILE)  # 原子写入
        except Exception as e:
            print(f'⚠️ 持久化失败: {e}')

def cleanup_offline():
    """后台清理离线服务器"""
    while True:
        now = time.time()
        with lock:
            offline = [k for k, v in servers.items() if now - v['last_seen'] > OFFLINE_TIMEOUT * 3]
            for k in offline:
                del servers[k]
        time.sleep(60)

# ============ 前端页面 ============

HTML_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ServerProbe</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0e17;--bg2:#0f1520;--card:#141a26;--card2:#1a2132;
  --border:#1e2a3a;--border2:#253345;
  --text:#e8ecf1;--text2:#8d99ad;--text3:#5c6a7e;
  --accent:#7c5cfc;--accent2:#9b85ff;--accent-bg:rgba(124,92,252,.08);
  --green:#10b981;--green-bg:rgba(16,185,129,.08);
  --yellow:#f59e0b;--yellow-bg:rgba(245,158,11,.08);
  --red:#ef4444;--red-bg:rgba(239,68,68,.08);
  --blue:#3b82f6;--blue-bg:rgba(59,130,246,.08);
  --cyan:#06b6d4;--cyan-bg:rgba(6,182,212,.08);
  --r:14px;--rs:8px;
}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.5;min-height:100vh}
.wrap{max-width:1320px;margin:0 auto;padding:20px 16px}

/* Header */
.hdr{display:flex;align-items:center;justify-content:space-between;padding:16px 0 20px;flex-wrap:wrap;gap:12px}
.hdr-l{display:flex;align-items:center;gap:14px}
.logo{width:44px;height:44px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;box-shadow:0 0 20px rgba(124,92,252,.25)}
.hdr-title{font-size:21px;font-weight:700;letter-spacing:-.3px}
.hdr-sub{font-size:12px;color:var(--text3);margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;background:var(--green-bg);color:var(--green);font-size:12px;font-weight:600}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* Toolbar */
.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap}
.stats-bar{display:flex;gap:8px;flex-wrap:wrap}
.sb-item{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:8px 14px;display:flex;align-items:center;gap:6px;font-size:13px}
.sb-num{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.sb-label{color:var(--text3);font-size:11px}

/* Filter pills */
.filter-bar{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.filter-pill{padding:5px 12px;border-radius:16px;font-size:12px;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text2);transition:all .2s;font-weight:500}
.filter-pill:hover{border-color:var(--accent);color:var(--text)}
.filter-pill.active{background:var(--accent-bg);border-color:var(--accent);color:var(--accent)}

/* Sort */
.sort-sel{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:5px 10px;color:var(--text2);font-size:12px;cursor:pointer;outline:none}
.sort-sel:focus{border-color:var(--accent)}
.sort-sel option{background:var(--card2)}

/* Server card */
.srv-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);margin-bottom:12px;overflow:hidden;transition:border-color .2s}
.srv-card:hover{border-color:var(--border2)}
.srv-card.offline{opacity:.6}
.srv-head{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);cursor:pointer}
.srv-name{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:700}
.srv-status{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.srv-status.online{background:var(--green);box-shadow:0 0 8px rgba(16,185,129,.4)}
.srv-status.offline{background:var(--red);box-shadow:0 0 8px rgba(239,68,68,.4)}
.srv-meta{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--text2);flex-wrap:wrap}
.srv-tag{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.3px}
.srv-body{padding:14px 18px;display:none}
.srv-card.open .srv-body{display:block}

/* Grid inside server */
.sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-bottom:10px}

/* Mini cards */
.mc{background:var(--bg2);border:1px solid var(--border);border-radius:var(--rs);padding:10px 12px}
.mc-label{font-size:10px;color:var(--text3);margin-bottom:3px;text-transform:uppercase;letter-spacing:.3px}
.mc-val{font-size:18px;font-weight:700;letter-spacing:-.5px;line-height:1.2}
.mc-detail{font-size:10px;color:var(--text3);margin-top:2px}

/* Progress */
.pw{background:rgba(255,255,255,.04);border-radius:4px;height:4px;overflow:hidden;margin:5px 0 2px}
.pb{height:100%;border-radius:4px;transition:width .5s ease,background .3s}

/* Rows */
.sr{display:flex;justify-content:space-between;align-items:center;padding:2px 0;font-size:11px}
.sk{color:var(--text3)}.sv{font-weight:600;font-variant-numeric:tabular-nums}

/* Colors */
.tg{color:var(--green)}.ty{color:var(--yellow)}.tr{color:var(--red)}.tb{color:var(--blue)}.tc{color:var(--cyan)}.ta{color:var(--accent)}
.bg-green{background:var(--green-bg);color:var(--green)}
.bg-yellow{background:var(--yellow-bg);color:var(--yellow)}
.bg-red{background:var(--red-bg);color:var(--red)}
.bg-blue{background:var(--blue-bg);color:var(--blue)}
.bg-accent{background:var(--accent-bg);color:var(--accent)}
.bg-cyan{background:var(--cyan-bg);color:var(--cyan)}

/* Net table */
.nt{width:100%;font-size:11px;border-collapse:collapse}
.nt th{text-align:left;color:var(--text3);font-weight:500;padding:3px 0;font-size:9px;text-transform:uppercase;letter-spacing:.5px}
.nt td{padding:3px 0;font-variant-numeric:tabular-nums}
.nt tr{border-bottom:1px solid rgba(255,255,255,.03)}

/* Disk item */
.di{padding:4px 0;border-bottom:1px solid rgba(255,255,255,.03)}
.di:last-child{border:none}
.di-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px;font-size:11px}

/* Empty */
.empty{text-align:center;padding:60px 20px;color:var(--text3)}
.empty-icon{font-size:48px;margin-bottom:16px}
.empty-text{font-size:16px;margin-bottom:8px}
.empty-sub{font-size:13px;color:var(--text3);max-width:500px;margin:0 auto;line-height:1.7}

/* Footer */
.ft{text-align:center;padding:20px 0;font-size:11px;color:var(--text3)}

/* Responsive */
@media(max-width:768px){
  .wrap{padding:12px 10px}
  .sg{grid-template-columns:1fr 1fr}
  .toolbar{flex-direction:column;align-items:stretch}
  .filter-bar{margin-left:0}
}
@media(max-width:480px){
  .sg{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div class="hdr-l">
      <div class="logo">⚡</div>
      <div>
        <div class="hdr-title">ServerProbe</div>
        <div class="hdr-sub">多服务器集中监控</div>
      </div>
    </div>
    <div class="badge"><span class="dot"></span><span id="clock"></span></div>
  </div>

  <div class="toolbar">
    <div class="stats-bar" id="stats-bar"></div>
    <select class="sort-sel" id="sort-sel" onchange="go()">
      <option value="name">按名称</option>
      <option value="cpu">按 CPU</option>
      <option value="mem">按内存</option>
      <option value="load">按负载</option>
      <option value="status">在线优先</option>
    </select>
    <div class="filter-bar" id="filter-bar"></div>
  </div>

  <div id="servers"></div>
  <div class="ft">⚡ ServerProbe · <span id="ri"></span>s 刷新</div>
</div>

<script>
const R=3;
let allData={};
let activeFilter='__all__';

function F(b){if(!b)return'0 B';const u=['B','KB','MB','GB','TB'];const i=Math.min(Math.floor(Math.log(b)/Math.log(1024)),u.length-1);return(b/Math.pow(1024,i)).toFixed(i?1:0)+' '+u[i]}
function FS(b){if(!b||b<=0)return'0 B/s';const u=['B/s','KB/s','MB/s','GB/s'];const i=Math.min(Math.floor(Math.log(b)/Math.log(1024)),u.length-1);return(b/Math.pow(1024,i)).toFixed(i?1:0)+' '+u[i]}
function UT(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return(d?d+'天 ':'')+(h?h+'时 ':'')+m+'分'}
function BC(p){return p<60?'var(--green)':p<85?'var(--yellow)':'var(--red)'}
function TC(p){return p<60?'tg':p<85?'ty':'tr'}
function ago(ts){const s=Math.floor(Date.now()/1000-ts);if(s<60)return s+'秒前';if(s<3600)return Math.floor(s/60)+'分钟前';return Math.floor(s/3600)+'小时前'}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}

function render(data){
  const servers=Object.values(data);
  const online=servers.filter(s=>s.online).length;
  const total=servers.length;

  // Clock
  document.getElementById('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour12:false});

  // Stats bar
  document.getElementById('stats-bar').innerHTML=`
    <div class="sb-item"><span class="sb-num tg">${online}</span><span class="sb-label">在线</span></div>
    <div class="sb-item"><span class="sb-num tr">${total-online}</span><span class="sb-label">离线</span></div>
    <div class="sb-item"><span class="sb-num">${total}</span><span class="sb-label">总计</span></div>
  `;

  // Filter pills (tags)
  const allTags=new Set();
  servers.forEach(s=>{(s.data.tags||[]).forEach(t=>allTags.add(t))});
  let fh=`<span class="filter-pill ${activeFilter==='__all__'?'active':''}" onclick="setFilter('__all__')">全部</span>`;
  [...allTags].sort().forEach(t=>{
    fh+=`<span class="filter-pill ${activeFilter===t?'active':''}" onclick="setFilter('${esc(t)}')">${esc(t)}</span>`;
  });
  document.getElementById('filter-bar').innerHTML=fh;

  if(!total){
    document.getElementById('servers').innerHTML=`
      <div class="empty"><div class="empty-icon">📡</div>
      <div class="empty-text">暂无服务器</div>
      <div class="empty-sub">在每台VPS上运行 Agent 即可开始监控</div></div>`;
    return;
  }

  // Filter
  let filtered=servers;
  if(activeFilter!=='__all__'){
    filtered=servers.filter(s=>(s.data.tags||[]).includes(activeFilter));
  }

  // Sort
  const sortBy=document.getElementById('sort-sel').value;
  filtered.sort((a,b)=>{
    switch(sortBy){
      case 'cpu': return b.data.cpu.usage-a.data.cpu.usage;
      case 'mem': return b.data.memory.usage_percent-a.data.memory.usage_percent;
      case 'load': return b.data.load.load_1-a.data.load.load_1;
      case 'status': return b.online-a.online;
      default: return a.data.name.localeCompare(b.data.name);
    }
  });

  let html='';
  for(const srv of filtered){
    const st=srv.online?'online':'offline';
    const d=srv.data;
    const cu=d.cpu.usage, mp=d.memory.usage_percent;
    const tags=(d.tags||[]).map(t=>`<span class="srv-tag bg-accent">${esc(t)}</span>`).join('');
    const ns=d.network_speeds||{};

    html+=`<div class="srv-card ${srv.online?'':'offline'} open" id="srv-${esc(d.name).replace(/[^a-zA-Z0-9]/g,'_')}">
      <div class="srv-head" onclick="this.parentElement.classList.toggle('open')">
        <div class="srv-name"><span class="srv-status ${st}"></span>${esc(d.name)} ${tags}</div>
        <div class="srv-meta">
          <span class="srv-tag ${srv.online?'bg-green':'bg-red'}">${srv.online?'在线':'离线'}</span>
          <span>${esc(d.os.distro||d.os.os)}</span>
          <span>↑ ${UT(d.load.uptime)}</span>
          <span>${ago(d.timestamp)}</span>
        </div>
      </div>
      <div class="srv-body">
        <div class="sg">
          <div class="mc">
            <div class="mc-label">🖥 CPU</div>
            <div class="mc-val ${TC(cu)}">${cu}%</div>
            <div class="pw"><div class="pb" style="width:${cu}%;background:${BC(cu)}"></div></div>
            <div class="sr"><span class="sk">${esc(d.cpu.model)}</span></div>
            <div class="sr"><span class="sk">${d.cpu.cores}核 · ${d.cpu.temperature!==null?d.cpu.temperature+'°C':'N/A'}</span></div>
          </div>
          <div class="mc">
            <div class="mc-label">💾 内存</div>
            <div class="mc-val ${TC(mp)}">${mp}%</div>
            <div class="mc-detail">${F(d.memory.used)} / ${F(d.memory.total)}</div>
            <div class="pw"><div class="pb" style="width:${mp}%;background:${BC(mp)}"></div></div>
            <div class="sr"><span class="sk">Swap</span><span class="sv">${d.memory.swap_total>0?F(d.memory.swap_used)+'/'+F(d.memory.swap_total):'未启用'}</span></div>
          </div>
          <div class="mc">
            <div class="mc-label">📊 负载</div>
            <div class="mc-val">${d.load.load_1.toFixed(2)}</div>
            <div class="mc-detail">5m: ${d.load.load_5.toFixed(2)} · 15m: ${d.load.load_15.toFixed(2)}</div>
            <div class="sr"><span class="sk">进程</span><span class="sv">${d.load.processes}</span></div>
          </div>
          <div class="mc">
            <div class="mc-label">🐧 系统</div>
            <div class="mc-val" style="font-size:13px">${esc(d.os.distro||d.os.os)}</div>
            <div class="sr"><span class="sk">内核</span><span class="sv">${esc(d.os.kernel)}</span></div>
            <div class="sr"><span class="sk">虚拟化</span><span class="sv">${esc(d.virtualization)}</span></div>
          </div>
        </div>
        <div style="margin-bottom:8px">
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">💿 磁盘</div>
          ${d.disk.map(dk=>`<div class="di"><div class="di-top"><span>${esc(dk.mount)}</span><span class="${TC(dk.usage_percent)}">${dk.usage_percent}% · ${F(dk.used)}/${F(dk.total)}</span></div><div class="pw"><div class="pb" style="width:${dk.usage_percent}%;background:${BC(dk.usage_percent)}"></div></div></div>`).join('')}
        </div>
        <div>
          <div style="font-size:11px;font-weight:600;color:var(--text3);margin-bottom:4px">🌐 网络</div>
          <table class="nt"><tr><th>接口</th><th>↓ 接收</th><th>↑ 发送</th><th>↓ 速率</th><th>↑ 速率</th></tr>
          ${d.network.map(i=>{const sp=ns[i.interface]||{};
            return `<tr><td class="tc">${esc(i.interface)}</td><td>${F(i.rx_bytes)}</td><td>${F(i.tx_bytes)}</td><td class="tg">${FS(sp.rx_speed||0)}</td><td class="tb">${FS(sp.tx_speed||0)}</td></tr>`;
          }).join('')}
          </table>
        </div>
      </div>
    </div>`;
  }
  document.getElementById('servers').innerHTML=html;
}

function setFilter(tag){
  activeFilter=tag;
  render(allData);
}

async function go(){
  try{
    const r=await fetch('/api/servers');allData=await r.json();render(allData);
  }catch(e){console.error(e)}
}
document.getElementById('ri').textContent=R;
go();setInterval(go,R*1000);
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
            self.wfile.write(HTML_PAGE.encode('utf-8'))
        elif path == '/api/servers':
            now = time.time()
            result = {}
            with lock:
                for name, srv in servers.items():
                    result[name] = {
                        'data': srv['data'],
                        'online': now - srv['last_seen'] < OFFLINE_TIMEOUT,
                        'last_seen': srv['last_seen'],
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

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
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
    threading.Thread(target=cleanup_offline, daemon=True).start()
    if PERSIST_FILE:
        threading.Thread(target=persist_loop, daemon=True).start()

    print(f'''
⚡ ServerProbe Dashboard 启动
   地址: http://0.0.0.0:{PORT}
   离线判定: {OFFLINE_TIMEOUT}秒
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
