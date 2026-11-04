#!/usr/bin/env python3
"""Small Linux web panel for a single Paper Minecraft server."""

from __future__ import annotations

import http.cookies
import fcntl
import importlib.util
import json
import os
import re
import signal
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import uuid

import docker_manager as docker_backend


DATA = Path(os.environ.get("MINESERVE_DATA", "/data")).resolve()
SERVER_DIR = DATA / "server"
PORT = int(os.environ.get("WEB_PORT", "8080"))
PANEL_BIND = os.environ.get("PANEL_BIND", "0.0.0.0")
USERNAME = os.environ.get("MINESERVE_USER", "admin")
PASSWORD = os.environ.get("MINESERVE_PASSWORD", "")
EXPOSURE = os.environ.get("MINESERVE_EXPOSURE", "lan")
NGROK_TOKEN = os.environ.get("NGROK_AUTHTOKEN", "")
MAX_RAM = int(os.environ.get("MAX_RAM_MB", "8192"))
LOCK = threading.RLock()
LOGS: deque[str] = deque(maxlen=1500)
SESSIONS: dict[str, float] = {}
PROCESS: subprocess.Popen[str] | None = None
TUNNEL_PROCESS: subprocess.Popen[str] | None = None
TUNNEL_LOGS: deque[str] = deque(maxlen=100)
SERVER_TUNNELS: dict[str, subprocess.Popen[str]] = {}
SERVER_TUNNEL_LOGS: dict[str, deque[str]] = {}
INSTANCE_LOCK = None
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.RLock()
LOGIN_FAILURES: dict[str, list[float]] = {}
WEB_HTML = Path(__file__).with_name("webpanel.html")


def submit_job(kind: str, operation):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "kind": kind, "status": "running", "message": "Đang xử lý", "result": None}

    def run():
        try:
            result = operation()
            with JOBS_LOCK:
                JOBS[job_id].update(status="complete", message="Hoàn tất", result=result)
        except Exception as exc:
            with JOBS_LOCK:
                JOBS[job_id].update(status="failed", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return job_id


def load_state() -> dict:
    try:
        return json.loads((DATA / "panel.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"ram_mb": min(2048, MAX_RAM), "version": "", "port": 25565}


def save_state(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "panel.json").write_text(json.dumps(state, indent=2))


STATE = load_state()


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with LOCK:
        LOGS.append(line)
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            log_path = DATA / "panel.log"
            if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
                rotated = DATA / "panel.log.1"
                try:
                    rotated.unlink()
                except FileNotFoundError:
                    pass
                log_path.replace(rotated)
            with log_path.open("a", encoding="utf-8") as output:
                output.write(line + "\n")
        except OSError:
            pass


def start_tunnel() -> None:
    global TUNNEL_PROCESS
    if EXPOSURE not in {"ngrok", "playit", "bore"}:
        return
    executable = {"ngrok": "ngrok", "playit": "playit", "bore": "bore"}[EXPOSURE]
    binary = shutil.which(executable)
    if not binary:
        log(f"Tunnel is set to {EXPOSURE}, but '{executable}' is not installed on the host")
        return
    try:
        if EXPOSURE == "ngrok":
            cfg = DATA / "ngrok.yml"
            config = 'version: "3"\n'
            if NGROK_TOKEN:
                config += "agent:\n  authtoken: " + json.dumps(NGROK_TOKEN) + "\n"
            config += f"endpoints:\n  - name: mineserve-admin\n    upstream:\n      url: http://127.0.0.1:{PORT}\n"
            cfg.write_text(config)
            cfg.chmod(0o600)
            command = [binary, "start", "--all", "--config", str(cfg), "--log", "stdout"]
        elif EXPOSURE == "playit":
            command = [binary]
        else:
            command = [binary, "local", str(PORT), "--to", "bore.pub"]
        TUNNEL_PROCESS = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, bufsize=1,
                                          start_new_session=True)
        threading.Thread(target=tail_tunnel, args=(TUNNEL_PROCESS.stdout,), daemon=True).start()
        log(f"Starting {EXPOSURE} tunnel")
    except OSError as exc:
        log(f"Could not start {EXPOSURE} tunnel: {exc}")


def tail_tunnel(stream) -> None:
    for line in iter(stream.readline, ""):
        value = line.rstrip()
        with LOCK:
            TUNNEL_LOGS.append(value)
        log(f"[{EXPOSURE}] {value}")


def _server_tunnel_log(server_id, stream):
    lines = SERVER_TUNNEL_LOGS.setdefault(server_id, deque(maxlen=100))
    for line in iter(stream.readline, ""):
        value = line.rstrip()
        lines.append(value)
        log(f"[server-tunnel:{server_id}] {value}")


def server_tunnel_status(server_id):
    process = SERVER_TUNNELS.get(server_id)
    lines = list(SERVER_TUNNEL_LOGS.get(server_id, ()))
    address = ""
    for line in reversed(lines):
        match = re.search(r"([A-Za-z0-9_.-]+\.(?:bore\.pub|ply\.gg|joinmc\.link|playit\.gg)(?::\d+)?)", line, re.I)
        if not match:
            match = re.search(r"((?:\d{1,3}\.){3}\d{1,3}:\d+)", line)
        if not match:
            match = re.search(r"(?:listening at)[: ]+([\w.-]+:\d+)", line, re.I)
        if match:
            address = match.group(1)
            break
    return {"running": process is not None and process.poll() is None, "address": address, "logs": lines[-20:]}


def start_server_tunnel(server_id):
    server = docker_backend.get_server(server_id)
    if not server:
        raise ValueError("Không tìm thấy server")
    if server["status"] != "RUNNING":
        raise ValueError("Khởi động Minecraft server trước khi mở tunnel")
    current = SERVER_TUNNELS.get(server_id)
    if current is not None and current.poll() is None:
        return server_tunnel_status(server_id)
    cli_file = next((p for p in (Path(__file__).with_name("cli.py"), Path(__file__).resolve().parents[1] / "linux/cli.py") if p.is_file()), None)
    if not cli_file:
        raise RuntimeError("Không tìm thấy MineServe tunnel installer")
    spec = importlib.util.spec_from_file_location("mineserve_linux_cli", cli_file)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    config = docker_backend.get_tunnel_config(server_id)
    provider = config.get("provider", "bore")
    install_mode = "bore" if provider == "custom_bore" else provider
    cli.install_tunnel(install_mode)
    binary = shutil.which(install_mode) or str(cli.BIN_DIR / install_mode)
    if provider in {"bore", "custom_bore"}:
        relay = "bore.pub" if provider == "bore" else f"{config.get('relayHost', 'bore.pub')}:{config.get('relayPort', 7835)}"
        command = [binary, "local", str(server["port"]), "--to", relay]
    else:
        command = [binary, "--stdout"]
        if config.get("hasSecret"):
            _, server_record = docker_backend._require_server(server_id)
            secret = server_record.get("tunnel", {}).get("secret", "")
            if secret:
                command.extend(["--secret", secret])
        command.append("start")
    process = subprocess.Popen(command,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, start_new_session=True)
    SERVER_TUNNELS[server_id] = process
    SERVER_TUNNEL_LOGS[server_id] = deque(maxlen=100)
    threading.Thread(target=_server_tunnel_log, args=(server_id, process.stdout), daemon=True).start()
    return server_tunnel_status(server_id)


def stop_server_tunnel(server_id):
    process = SERVER_TUNNELS.pop(server_id, None)
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    return server_tunnel_status(server_id)


def stop_tunnel() -> None:
    global TUNNEL_PROCESS
    proc = TUNNEL_PROCESS
    TUNNEL_PROCESS = None
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def alive() -> bool:
    return PROCESS is not None and PROCESS.poll() is None


def tail_process(stream) -> None:
    for line in iter(stream.readline, ""):
        log(line.rstrip())


def start_server() -> None:
    global PROCESS
    with LOCK:
        if alive():
            return
        jar = SERVER_DIR / "server.jar"
        if not jar.is_file():
            raise ValueError("Install Paper before starting the server")
        if not (SERVER_DIR / "eula.txt").is_file() or "eula=true" not in (SERVER_DIR / "eula.txt").read_text():
            raise ValueError("Accept the Minecraft EULA in the panel first")
        ram = max(512, min(int(STATE["ram_mb"]), MAX_RAM))
        version = str(STATE.get("version", ""))
        try:
            major_version = int(version.split(".", 1)[0])
        except ValueError:
            major_version = 0
        java = "/opt/java/openjdk/bin/java" if major_version >= 26 else "/opt/java/openjdk-21/bin/java"
        PROCESS = subprocess.Popen(
            [java, f"-Xms{ram}M", f"-Xmx{ram}M", "-jar", "server.jar", "nogui"],
            cwd=SERVER_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=tail_process, args=(PROCESS.stdout,), daemon=True).start()
        log("Starting Minecraft server")


def stop_server() -> None:
    with LOCK:
        proc = PROCESS
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            proc.wait(timeout=60)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log("Minecraft server stopped")


def install_paper(version: str) -> None:
    if not version or len(version) > 32 or any(c not in "0123456789.-" for c in version):
        raise ValueError("Choose a valid Minecraft version")
    encoded = urllib.parse.quote(version, safe="")
    if tuple(int(part) for part in version.split(".")) < (1, 20):
        raise ValueError("Linux edition hiện hỗ trợ Paper Minecraft 1.20 trở lên")
    api = f"https://fill.papermc.io/v3/projects/paper/versions/{encoded}/builds"
    user_agent = "MineServe-Docker/1.0 (https://github.com/devwithzachary/MineServe)"
    req = urllib.request.Request(api, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as response:
        builds = json.load(response)
    if not builds:
        raise ValueError("No Paper build is available for this Minecraft version")
    build = next((item for item in builds if item.get("channel") == "STABLE"), None)
    if not build:
        raise ValueError("Paper không có bản stable cho phiên bản Minecraft này")
    download = build["downloads"]["server:default"]["url"]
    req = urllib.request.Request(download, headers={"User-Agent": user_agent})
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    temp = SERVER_DIR / "server.jar.tmp"
    with urllib.request.urlopen(req, timeout=120) as response, temp.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
    temp.replace(SERVER_DIR / "server.jar")
    STATE["version"] = version
    save_state(STATE)
    log(f"Installed Paper {version}, build {build['build']}")


HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MineServe</title><style>
:root{color-scheme:dark;--bg:#0b1120;--card:#1e293b;--line:#334155;--green:#10b981;--muted:#94a3b8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f8fafc;font:15px system-ui,sans-serif}header{padding:18px 5vw;background:#111827;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}h1{margin:0;color:#34d399;font-size:22px}main{max-width:1100px;margin:24px auto;padding:0 18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}h2{margin:0 0 14px;font-size:18px}label{display:block;color:var(--muted);margin:12px 0 5px}input,button{font:inherit;border:1px solid var(--line);border-radius:8px;padding:10px;background:#0f172a;color:inherit}input{width:100%}button{cursor:pointer;background:var(--green);color:#052e24;border:0;font-weight:700;margin:8px 6px 0 0}button.secondary{background:#334155;color:white}button.danger{background:#b91c1c;color:white}.status{font-weight:700;color:#34d399}pre{white-space:pre-wrap;overflow:auto;background:#050811;border-radius:8px;padding:12px;height:310px;color:#cbd5e1}#files{max-height:360px;overflow:auto}a{color:#34d399;cursor:pointer}small,.muted{color:var(--muted)}.row{display:flex;gap:8px;align-items:center}.row input{flex:1}#login{max-width:420px;margin:12vh auto}.hidden{display:none}</style></head><body><header><h1>MineServe <small>Linux Server</small></h1><button id="logout" class="secondary hidden">Đăng xuất</button></header>
<main><section id="login" class="card"><h2>Đăng nhập</h2><label>Tên đăng nhập</label><input id="username" autocomplete="username"><label>Mật khẩu panel</label><input id="password" type="password" autocomplete="current-password"><button onclick="login()">Đăng nhập</button><p id="loginError" class="muted"></p></section>
<section id="app" class="hidden"><div class="grid"><div class="card"><h2>Trạng thái server</h2><p id="status" class="status">Đang tải…</p><p id="meta" class="muted"></p><button onclick="action('start')">Khởi chạy</button><button class="secondary" onclick="action('restart')">Khởi động lại</button><button class="danger" onclick="action('stop')">Dừng an toàn</button></div>
<div class="card"><h2>Cài đặt Paper</h2><label>Phiên bản Minecraft</label><input id="version" placeholder="Ví dụ: 1.21.4"><label>RAM cấp cho Java (MB)</label><input id="ram" type="number" min="512" step="256"><label><input id="eula" type="checkbox" style="width:auto"> Tôi đồng ý với <a href="https://www.minecraft.net/eula" target="_blank" rel="noreferrer">Minecraft EULA</a></label><button onclick="install()">Tải / cập nhật Paper</button></div></div>
<div class="card"><h2>Console</h2><pre id="logs"></pre><div class="row"><input id="command" placeholder="Nhập lệnh server…" onkeydown="if(event.key==='Enter')sendCommand()"><button onclick="sendCommand()">Gửi</button></div></div>
<div class="grid"><div class="card"><h2>server.properties</h2><textarea id="properties" style="width:100%;height:240px;font:14px monospace;background:#0f172a;color:white;border:1px solid var(--line);border-radius:8px;padding:10px"></textarea><button onclick="saveProperties()">Lưu cấu hình</button></div><div class="card"><h2>Tệp server</h2><div id="files"></div><input id="upload" type="file" accept=".txt,.properties,.yml,.yaml,.json,.toml,.cfg,.conf,.md" style="margin-top:14px"><button onclick="uploadFile()">Tải tệp lên thư mục hiện tại</button><p class="muted">Chỉ chỉnh sửa tệp văn bản. Thư mục world không được hiển thị trong danh sách.</p></div></div></section></main>
<script>
const $=id=>document.getElementById(id);let currentPath='';async function api(path,opts={}){const r=await fetch('/api/'+path,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Yêu cầu thất bại');return d}async function login(){try{await api('login',{method:'POST',body:JSON.stringify({username:$('username').value,password:$('password').value})});show();refresh()}catch(e){$('loginError').textContent=e.message}}function show(){ $('login').classList.add('hidden');$('app').classList.remove('hidden');$('logout').classList.remove('hidden')}$('logout').onclick=async()=>{await api('logout',{method:'POST'});location.reload()};async function init(){try{await api('me');show();refresh()}catch{}}async function refresh(){try{const d=await api('status');$('status').textContent=d.running?'ĐANG CHẠY':'ĐÃ DỪNG';$('meta').textContent=`Paper ${d.version||'chưa cài'} · Cổng ${d.port} · RAM ${d.ram_mb} MB`;$('ram').value=d.ram_mb;$('version').value=d.version||'';$('logs').textContent=d.logs.join('\n');$('logs').scrollTop=$('logs').scrollHeight;await loadFiles();await loadProperties()}catch(e){console.error(e)}}async function action(a){try{await api(a,{method:'POST'});setTimeout(refresh,600)}catch(e){alert(e.message)}}async function install(){if(!$('eula').checked){alert('Vui lòng đồng ý Minecraft EULA trước khi cài.');return}try{await api('install',{method:'POST',body:JSON.stringify({version:$('version').value,ram_mb:+$('ram').value,eula:true})});alert('Đã cài Paper');refresh()}catch(e){alert(e.message)}}async function sendCommand(){const command=$('command').value;if(!command)return;try{await api('command',{method:'POST',body:JSON.stringify({command})});$('command').value='';refresh()}catch(e){alert(e.message)}}async function loadProperties(){try{const d=await api('properties');$('properties').value=d.content}catch{ $('properties').value=''}}async function saveProperties(){try{await api('properties',{method:'PUT',body:JSON.stringify({content:$('properties').value})});alert('Đã lưu')}catch(e){alert(e.message)}}async function loadFiles(){const d=await api('files?path='+encodeURIComponent(currentPath));$('files').innerHTML=(currentPath?'<p><a onclick="currentPath='+JSON.stringify(currentPath.split('/').slice(0,-1).join('/'))+';refresh()">← Thư mục cha</a></p>':'')+d.entries.map(x=>`<div style="padding:6px;border-bottom:1px solid #334155">${x.dir?'📁':'📄'} <a onclick="${x.dir?'currentPath='+JSON.stringify(currentPath?currentPath+'/'+x.name:x.name)+';refresh()':'editFile('+JSON.stringify(x.name)+')'}">${escapeHtml(x.name)}</a> ${!x.dir?'<button class="danger" style="float:right;margin:0;padding:3px 8px" onclick="deleteFile('+JSON.stringify(x.name)+')">Xóa</button>':''}</div>`).join('')||'<small>Thư mục trống</small>'}function escapeHtml(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function editFile(n){try{const d=await api('file?path='+encodeURIComponent(currentPath?currentPath+'/'+n:n));const value=prompt('Nội dung tệp (chỉ hỗ trợ văn bản, tối đa 256 KB):',d.content);if(value!==null)await api('file',{method:'PUT',body:JSON.stringify({path:currentPath?currentPath+'/'+n:n,content:value})})}catch(e){alert(e.message)}refresh()}async function deleteFile(n){if(!confirm('Xóa '+n+'?'))return;try{await api('file',{method:'DELETE',body:JSON.stringify({path:currentPath?currentPath+'/'+n:n})});refresh()}catch(e){alert(e.message)}}async function uploadFile(){const f=$('upload').files[0];if(!f)return;const path=(currentPath?currentPath+'/':'')+f.name;try{await api('file',{method:'PUT',body:JSON.stringify({path,content:await f.text()})});refresh()}catch(e){alert('Panel chỉ nhận tệp văn bản ở phiên bản này: '+e.message)}}setInterval(()=>{if(!$('app').classList.contains('hidden'))refresh()},4000);init();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "MineServePanel/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data: dict | list, status: int = 200, headers: dict | None = None):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path, download_name: str):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def send_download(self, path: Path):
        size = path.stat().st_size
        filename = path.name.replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("Request body is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def session(self) -> bool:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("mineserve_session")
        if not token:
            return False
        expiry = SESSIONS.get(token.value, 0)
        if expiry < time.time():
            SESSIONS.pop(token.value, None)
            return False
        SESSIONS[token.value] = time.time() + 12 * 3600
        return True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path)
        if path.path in ("/", "/index.html"):
            try:
                raw = WEB_HTML.read_bytes()
            except OSError:
                raw = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-src 'self' http: https:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path.path == "/api/me":
            self.send_json({"authenticated": self.session()}, 200 if self.session() else 401)
            return
        if not self.session():
            self.send_json({"error": "Vui lòng đăng nhập"}, 401)
            return
        try:
            if path.path == "/api/platforms":
                self.send_json({"platforms": [{"id": key, **value} for key, value in docker_backend.PLATFORMS.items()]})
            elif path.path.startswith("/api/platforms/") and path.path.endswith("/versions"):
                platform_key = path.path.split("/")[3]
                self.send_json({"versions": docker_backend._version_options(platform_key)})
            elif path.path == "/api/servers":
                self.send_json({"servers": docker_backend.list_servers()})
            elif path.path.startswith("/api/servers/"):
                segments = path.path.strip("/").split("/")
                server_id = segments[2] if len(segments) > 2 else ""
                server = docker_backend.get_server(server_id)
                if server is None:
                    self.send_json({"error": "Không tìm thấy server"}, 404)
                elif len(segments) == 3:
                    self.send_json(server)
                elif len(segments) == 4 and segments[3] == "console":
                    self.send_json(docker_backend.server_logs(server_id))
                elif len(segments) == 4 and segments[3] == "diagnostics":
                    self.send_json(docker_backend.analyze_server_crash(server_id))
                elif len(segments) == 4 and segments[3] == "metrics":
                    self.send_json(docker_backend.server_metrics(server_id))
                elif len(segments) == 4 and segments[3] == "port-forward":
                    self.send_json(docker_backend.port_forward_status(server_id))
                elif len(segments) == 4 and segments[3] == "tunnel":
                    self.send_json(server_tunnel_status(server_id))
                elif len(segments) == 4 and segments[3] == "tunnel-config":
                    self.send_json(docker_backend.get_tunnel_config(server_id))
                elif len(segments) == 4 and segments[3] == "files":
                    relative = urllib.parse.parse_qs(path.query).get("path", [""])[0]
                    self.send_json(docker_backend.list_files(server_id, relative))
                elif len(segments) == 4 and segments[3] == "file-search":
                    query = urllib.parse.parse_qs(path.query).get("q", [""])[0]
                    self.send_json(docker_backend.search_server_files(server_id, query))
                elif len(segments) == 4 and segments[3] == "backups":
                    self.send_json({"backups": docker_backend.list_backups(server_id)})
                elif len(segments) == 4 and segments[3] == "properties":
                    self.send_json(docker_backend.read_properties(server_id))
                elif len(segments) == 4 and segments[3] == "players":
                    self.send_json(docker_backend.player_lists(server_id))
                elif len(segments) == 4 and segments[3] == "world":
                    self.send_json(docker_backend.world_summary(server_id))
                elif len(segments) == 4 and segments[3] == "automation":
                    self.send_json(docker_backend.get_automation(server_id))
                elif len(segments) == 4 and segments[3] == "cloud-backup":
                    self.send_json(docker_backend.get_cloud_config(server_id))
                elif len(segments) == 4 and segments[3] == "modrinth":
                    query = urllib.parse.parse_qs(path.query)
                    self.send_json(docker_backend.modrinth_search(server_id, query.get("q", [""])[0], query.get("type", [None])[0]))
                elif len(segments) == 5 and segments[3] == "backups":
                    filename = segments[4]
                    backup = next((item for item in docker_backend.list_backups(server_id) if item["name"] == filename), None)
                    if not backup:
                        self.send_json({"error": "Không tìm thấy backup"}, 404)
                    else:
                        self.send_file(docker_backend.DATA / "backups" / server_id / filename, filename)
                elif len(segments) == 5 and segments[3] == "file" and segments[4] == "read":
                    relative = urllib.parse.parse_qs(path.query).get("path", [""])[0]
                    self.send_json({"path": relative, "content": docker_backend.read_file(server_id, relative)})
                elif len(segments) == 5 and segments[3] == "file" and segments[4] == "download":
                    relative = urllib.parse.parse_qs(path.query).get("path", [""])[0]
                    _, target = docker_backend.server_files(server_id, relative)
                    if not target.is_file() or target.is_symlink():
                        self.send_json({"error": "Không tìm thấy file"}, 404)
                    else:
                        self.send_download(target)
                else:
                    self.send_json({"error": "Không tìm thấy"}, 404)
            elif path.path == "/api/runtime":
                self.send_json(docker_backend.runtime_status())
            elif path.path.startswith("/api/jobs/"):
                job_id = path.path.rsplit("/", 1)[-1]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                self.send_json(job or {"error": "Không tìm thấy job"}, 200 if job else 404)
            elif path.path == "/api/status":
                with LOCK:
                    self.send_json({**STATE, "running": alive(), "exposure": EXPOSURE,
                                    "tunnel_running": TUNNEL_PROCESS is not None and TUNNEL_PROCESS.poll() is None,
                                    "logs": list(LOGS)[-300:]})
            elif path.path == "/api/properties":
                prop = SERVER_DIR / "server.properties"
                self.send_json({"content": prop.read_text() if prop.exists() else ""})
            elif path.path == "/api/files":
                target = self.safe_path(urllib.parse.parse_qs(path.query).get("path", [""])[0])
                target.mkdir(parents=True, exist_ok=True)
                entries = [{"name": p.name, "dir": p.is_dir()} for p in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())) if not p.name.startswith(".") and p.name not in {"world", "world_nether", "world_the_end"}]
                self.send_json({"entries": entries})
            elif path.path == "/api/file":
                target = self.safe_path(urllib.parse.parse_qs(path.query).get("path", [""])[0])
                if target.stat().st_size > 256 * 1024:
                    raise ValueError("Tệp lớn hơn 256 KB")
                self.send_json({"content": target.read_text()})
            else:
                self.send_json({"error": "Không tìm thấy"}, 404)
        except (OSError, ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            data = self.body()
            if path == "/api/login":
                client_ip = self.client_address[0]
                now = time.monotonic()
                recent = [stamp for stamp in LOGIN_FAILURES.get(client_ip, []) if now - stamp < 60]
                if len(recent) >= 5:
                    self.send_json({"error": "Quá nhiều lần đăng nhập sai; thử lại sau một phút"}, 429)
                    return
                user_ok = secrets.compare_digest(str(data.get("username", "")), USERNAME)
                pass_ok = secrets.compare_digest(str(data.get("password", "")), PASSWORD)
                if not (user_ok and pass_ok):
                    LOGIN_FAILURES[client_ip] = recent + [now]
                    self.send_json({"error": "Tên đăng nhập hoặc mật khẩu không đúng"}, 401)
                    return
                LOGIN_FAILURES.pop(client_ip, None)
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = time.time() + 12 * 3600
                self.send_json({"ok": True}, headers={"Set-Cookie": f"mineserve_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200"})
                return
            if path == "/api/logout":
                cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                if cookie.get("mineserve_session"):
                    SESSIONS.pop(cookie["mineserve_session"].value, None)
                self.send_json({"ok": True}, headers={"Set-Cookie": "mineserve_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
                return
            if not self.session():
                self.send_json({"error": "Vui lòng đăng nhập"}, 401)
                return
            if path == "/api/servers":
                job_id = submit_job("create-server", lambda: docker_backend.create_server(data))
                self.send_json({"jobId": job_id}, 202)
                return
            if path == "/api/runtime/reinstall":
                job_id = submit_job("runtime-reinstall", docker_backend.reinstall_runtime)
                self.send_json({"jobId": job_id}, 202)
                return
            if path.startswith("/api/runtime/java/"):
                java_version = path.rsplit("/", 1)[-1]
                job_id = submit_job(f"java-{java_version}", lambda: docker_backend.install_java(java_version))
                self.send_json({"jobId": job_id}, 202)
                return
            if path.startswith("/api/servers/"):
                segments = path.strip("/").split("/")
                server_id = segments[2] if len(segments) > 2 else ""
                action = segments[3] if len(segments) > 3 else ""
                if action in {"start", "stop", "restart", "delete"}:
                    if action in {"stop", "delete"}:
                        stop_server_tunnel(server_id)
                    result = docker_backend.server_action(server_id, action, bool(data.get("purgeData", False)))
                    if action == "start" and docker_backend.get_tunnel_config(server_id).get("autoStart"):
                        try:
                            start_server_tunnel(server_id)
                        except Exception as exc:
                            log(f"[server-tunnel:{server_id}] auto-start failed: {exc}")
                    self.send_json(result)
                    return
                if action == "resources":
                    self.send_json(docker_backend.update_server_resources(server_id, data))
                    return
                if action == "version":
                    job_id = submit_job("server-version-upgrade", lambda: docker_backend.update_server_version(server_id, data.get("version", ""), data.get("loaderVersion", "")))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "update-build":
                    job_id = submit_job("server-build-update", lambda: docker_backend.update_latest_build(server_id))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "port-forward":
                    self.send_json(docker_backend.enable_port_forward(server_id) if data.get("enabled") else docker_backend.disable_port_forward(server_id))
                    return
                if action == "tunnel":
                    self.send_json(start_server_tunnel(server_id) if data.get("enabled") else stop_server_tunnel(server_id))
                    return
                if action == "tunnel-config":
                    self.send_json(docker_backend.save_tunnel_config(server_id, data))
                    return
                if action == "backup":
                    job_id = submit_job("backup", lambda: docker_backend.create_backup(server_id, bool(data.get("worldOnly", False))))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "backups" and len(segments) == 6 and segments[5] == "restore":
                    filename = urllib.parse.unquote(segments[4])
                    job_id = submit_job("restore-backup", lambda: docker_backend.restore_backup(server_id, filename))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "modrinth" and len(segments) == 5 and segments[4] == "install":
                    project_id = str(data.get("projectId", ""))
                    job_id = submit_job("modrinth-install", lambda: docker_backend.install_modrinth_project(server_id, project_id))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "modrinth" and len(segments) == 5 and segments[4] == "pack":
                    relative = str(data.get("path", ""))
                    job_id = submit_job("modrinth-modpack", lambda: docker_backend.install_modrinth_pack(server_id, relative))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "world" and len(segments) == 5 and segments[4] == "reset":
                    self.send_json(docker_backend.reset_dimension(server_id, str(data.get("dimension", ""))))
                    return
                if action == "world" and len(segments) == 5 and segments[4] == "optimize":
                    job_id = submit_job("world-optimize", lambda: docker_backend.optimize_world(
                        server_id, data.get("thresholdTicks", 0), bool(data.get("pruneNether", True)), bool(data.get("pruneEnd", True))))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "world" and len(segments) == 5 and segments[4] == "import":
                    relative = str(data.get("path", ""))
                    _, source = docker_backend.server_files(server_id, relative)
                    job_id = submit_job("world-import", lambda: docker_backend.import_world(server_id, source))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "automation":
                    self.send_json(docker_backend.set_automation(server_id, data))
                    return
                if action == "cloud-backup":
                    self.send_json(docker_backend.save_cloud_config(server_id, data))
                    return
                if action == "cloud-sync" and len(segments) == 5:
                    job_id = submit_job("cloud-backup-sync", lambda: docker_backend.sync_backup_webdav(server_id, segments[4]))
                    self.send_json({"jobId": job_id}, 202)
                    return
                if action == "file-delete":
                    docker_backend.delete_file(server_id, str(data.get("path", "")))
                    self.send_json({"ok": True})
                    return
                if action == "file-create":
                    self.send_json(docker_backend.create_server_file(server_id, str(data.get("path", "")), False, str(data.get("content", ""))))
                    return
                if action == "directory-create":
                    self.send_json(docker_backend.create_server_file(server_id, str(data.get("path", "")), True))
                    return
                if action == "file-rename":
                    self.send_json(docker_backend.rename_server_file(server_id, str(data.get("path", "")), str(data.get("newName", ""))))
                    return
                if action == "file-duplicate":
                    self.send_json(docker_backend.duplicate_server_file(server_id, str(data.get("path", ""))))
                    return
                if action == "file-unzip":
                    self.send_json(docker_backend.unzip_server_file(server_id, str(data.get("path", "")), str(data.get("destination", ""))))
                    return
                if action == "command":
                    self.send_json(docker_backend.send_command(server_id, data.get("command", "")))
                    return
                self.send_json({"error": "Không tìm thấy"}, 404)
                return
            if path == "/api/install":
                if not data.get("eula"):
                    raise ValueError("Cần chấp nhận EULA")
                if alive():
                    raise ValueError("Hãy dừng server trước khi cài hoặc cập nhật")
                ram = int(data.get("ram_mb", 2048))
                if ram < 512 or ram > MAX_RAM:
                    raise ValueError(f"RAM phải từ 512 đến {MAX_RAM} MB")
                STATE["ram_mb"] = ram
                STATE["port"] = int(os.environ.get("MC_PORT", "25565"))
                SERVER_DIR.mkdir(parents=True, exist_ok=True)
                (SERVER_DIR / "eula.txt").write_text("eula=true\n")
                properties = SERVER_DIR / "server.properties"
                lines = properties.read_text().splitlines() if properties.exists() else []
                lines = [line for line in lines if not line.startswith("server-port=")]
                lines.append(f"server-port={STATE['port']}")
                properties.write_text("\n".join(lines) + "\n")
                save_state(STATE)
                install_paper(str(data.get("version", "")))
                self.send_json({"ok": True})
            elif path in ("/api/start", "/api/stop", "/api/restart"):
                if path == "/api/start":
                    start_server()
                elif path == "/api/stop":
                    stop_server()
                else:
                    stop_server()
                    start_server()
                self.send_json({"ok": True})
            elif path == "/api/command":
                command = str(data.get("command", ""))[:2048]
                if not command.strip() or "\n" in command or "\r" in command or not alive():
                    raise ValueError("Server chưa chạy hoặc lệnh không hợp lệ")
                PROCESS.stdin.write(command + "\n")
                PROCESS.stdin.flush()
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "Không tìm thấy"}, 404)
        except (OSError, ValueError, RuntimeError, KeyError, urllib.error.URLError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_PUT(self):
        if not self.session():
            self.send_json({"error": "Vui lòng đăng nhập"}, 401)
            return
        try:
            path = urllib.parse.urlsplit(self.path).path
            if self.headers.get("Content-Type", "").startswith("application/octet-stream") and path.startswith("/api/servers/"):
                segments = path.strip("/").split("/")
                server_id = segments[2] if len(segments) > 2 else ""
                relative = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("path", [""])[0]
                length = int(self.headers.get("Content-Length", "0"))
                if len(segments) == 5 and segments[3:] == ["world", "upload"]:
                    if Path(relative).suffix.lower() not in {".zip", ".mcworld"}:
                        raise ValueError("World import cần file .zip hoặc .mcworld")
                    target = docker_backend.write_binary_stream(server_id, relative, self.rfile, length, 4 * 1024 * 1024 * 1024)
                    root = Path(docker_backend._require_server(server_id)[1]["directory"])
                    self.send_json({"path": str(target.relative_to(root))})
                else:
                    docker_backend.write_binary_stream(server_id, relative, self.rfile, length)
                    self.send_json({"ok": True})
                return
            data = self.body()
            if path.startswith("/api/servers/"):
                segments = path.strip("/").split("/")
                server_id = segments[2] if len(segments) > 2 else ""
                if len(segments) == 5 and segments[3:] == ["file", "write"]:
                    docker_backend.write_file(server_id, str(data.get("path", "")), str(data.get("content", "")))
                    self.send_json({"ok": True})
                elif len(segments) == 4 and segments[3] == "properties":
                    self.send_json(docker_backend.write_properties(server_id, data.get("properties", {})))
                elif len(segments) == 4 and segments[3] == "automation":
                    self.send_json(docker_backend.set_automation(server_id, data))
                else:
                    self.send_json({"error": "Không tìm thấy"}, 404)
                return
            if path == "/api/properties":
                content = str(data.get("content", ""))
                if len(content) > 256 * 1024:
                    raise ValueError("Cấu hình quá lớn")
                SERVER_DIR.mkdir(parents=True, exist_ok=True)
                (SERVER_DIR / "server.properties").write_text(content)
            elif path == "/api/file":
                target = self.safe_path(str(data.get("path", "")))
                if target.name in {"eula.txt", "server.jar", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json"}:
                    raise ValueError("Không cho phép sửa tệp hệ thống hoặc phân quyền này")
                content = str(data.get("content", ""))
                if len(content.encode()) > 256 * 1024:
                    raise ValueError("Tệp lớn hơn 256 KB")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            else:
                self.send_json({"error": "Không tìm thấy"}, 404)
                return
            self.send_json({"ok": True})
        except (OSError, ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        if not self.session():
            self.send_json({"error": "Vui lòng đăng nhập"}, 401)
            return
        try:
            path = urllib.parse.urlsplit(self.path)
            if path.path.startswith("/api/servers/"):
                segments = path.path.strip("/").split("/")
                if len(segments) == 5 and segments[3] == "backups":
                    self.send_json(docker_backend.delete_backup(segments[2], urllib.parse.unquote(segments[4])))
                    return
                if len(segments) == 4 and segments[3] == "automation":
                    data = self.body()
                    self.send_json(docker_backend.delete_automation_task(segments[2], str(data.get("taskId", ""))))
                    return
            data = self.body()
            target = self.safe_path(str(data.get("path", "")))
            if target.name in {"eula.txt", "server.jar", "ops.json", "whitelist.json"} or target.is_dir():
                raise ValueError("Không thể xóa thư mục hoặc tệp hệ thống này")
            target.unlink()
            self.send_json({"ok": True})
        except (OSError, ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    @staticmethod
    def safe_path(relative: str) -> Path:
        target = (SERVER_DIR / relative).resolve()
        if target != SERVER_DIR and SERVER_DIR not in target.parents:
            raise ValueError("Đường dẫn không hợp lệ")
        return target


def main():
    global INSTANCE_LOCK
    DATA.mkdir(parents=True, exist_ok=True)
    if not PASSWORD or PASSWORD in {"changeme", "password"}:
        raise SystemExit("Set MINESERVE_PASSWORD to a strong, non-default password")
    INSTANCE_LOCK = (DATA / "panel.lock").open("w")
    try:
        fcntl.flock(INSTANCE_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("MineServe panel is already running for this user")
    (DATA / "panel.pid").write_text(str(os.getpid()))
    server = ThreadingHTTPServer((PANEL_BIND, PORT), Handler)
    server.daemon_threads = True
    log(f"Web panel listening on {PANEL_BIND}:{PORT}")
    docker_backend.start_automation_scheduler()
    start_tunnel()
    def shutdown_handler(_signum, _frame):
        threading.Thread(target=lambda: ([stop_server_tunnel(k) for k in list(SERVER_TUNNELS)], stop_server(), stop_tunnel(), server.shutdown()), daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    try:
        server.serve_forever()
    finally:
        stop_server()
        stop_tunnel()
        server.server_close()
        try:
            (DATA / "panel.pid").unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
