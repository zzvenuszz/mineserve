"""Docker-backed multi-server operations for MineServe Linux."""

from __future__ import annotations

import json
import http.client
import hashlib
import gzip
import struct
import zlib
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path


DATA = Path(os.environ.get("MINESERVE_DATA", str(Path.home() / ".local/share/mineserve"))).resolve()
SERVERS_DIR = DATA / "servers"
SERVER_INDEX = DATA / "servers.json"
IMAGE = "itzg/minecraft-server"
UA = "MineServe/1.2 (https://github.com/devwithzachary/MineServe)"
DOCKER_LOCK = threading.RLock()
VERSION_CACHE: dict[str, tuple[float, list[dict]]] = {}
STORAGE_CACHE: dict[str, tuple[float, dict]] = {}
METRICS_HISTORY: dict[str, dict[str, list]] = {}

PLATFORMS = {
    "PAPER": {"name": "PaperMC", "type": "PAPER", "java": 21, "plugins": True, "mods": False},
    "PURPUR": {"name": "Purpur", "type": "PURPUR", "java": 21, "plugins": True, "mods": False},
    "PAPER_GEYSER": {"name": "Paper + Geyser (Cross-play)", "type": "PAPER", "java": 21, "plugins": True, "mods": False},
    "VANILLA": {"name": "Vanilla", "type": "VANILLA", "java": 21, "plugins": False, "mods": False},
    "FABRIC": {"name": "Fabric", "type": "FABRIC", "java": 21, "plugins": False, "mods": True},
    "FORGE": {"name": "Forge", "type": "FORGE", "java": 21, "plugins": False, "mods": True},
    "NEOFORGE": {"name": "NeoForge", "type": "NEOFORGE", "java": 21, "plugins": False, "mods": True},
    "FOLIA": {"name": "Folia", "type": "FOLIA", "java": 21, "plugins": True, "mods": False},
}
JAVA_IMAGES = {8: "java8", 17: "java17", 21: "java21", 25: "java25"}


def _neoforge_game_version(loader_version):
    parts = str(loader_version).split(".")
    if parts and parts[0].isdigit() and int(parts[0]) >= 25:
        return ".".join(parts[:3])
    if len(parts) >= 4:
        return ".".join(parts[:3])
    if len(parts) >= 3:
        return f"1.{parts[0]}.{parts[1]}"
    return ""


class DockerError(RuntimeError):
    pass


def _request_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _sorted_versions(values):
    def key(v):
        bits = re.split(r"[.+_-]", str(v))
        return tuple((0, int(p)) if p.isdigit() else (1, p.lower()) for p in bits)
    return sorted(set(values), key=key, reverse=True)


def _version_options(platform: str) -> list[dict]:
    key = platform.upper()
    if key not in PLATFORMS:
        raise ValueError("Nền tảng server không hợp lệ")
    now = time.time()
    cached = VERSION_CACHE.get(key)
    if cached and now - cached[0] < 600:
        return cached[1]
    if key in {"PAPER", "PAPER_GEYSER", "FOLIA"}:
        project = "folia" if key == "FOLIA" else "paper"
        obj = _request_json(f"https://fill.papermc.io/v3/projects/{project}")
        raw = obj.get("versions", {})
        values = [item for group in raw.values() for item in group] if isinstance(raw, dict) else raw
    elif key == "PURPUR":
        values = _request_json("https://api.purpurmc.org/v2/purpur").get("versions", [])
    elif key == "VANILLA":
        manifest = _request_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        values = [item["id"] for item in manifest.get("versions", []) if item.get("type") in {"release", "snapshot"}]
    elif key == "FABRIC":
        values = [item["version"] for item in _request_json("https://meta.fabricmc.net/v2/versions/game") if item.get("version")]
    elif key == "FORGE":
        promotions = _request_json("https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json").get("promos", {})
        values = [name.rsplit("-", 1)[0] for name in promotions if name.endswith(("-recommended", "-latest"))]
    else:  # NeoForge maps Minecraft versions to the matching loader release.
        releases = _request_json("https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge").get("versions", [])
        loader_by_mc = {}
        for loader in releases:
            if "craftmine" in str(loader):
                continue
            mc = _neoforge_game_version(loader)
            if not mc:
                continue
            loader_by_mc[mc] = loader
        values = list(loader_by_mc)
    values = _sorted_versions(values)
    if key in {"PAPER", "PAPER_GEYSER", "FOLIA"}:
        # PaperMC's project API also lists pre-releases such as 26.3-rc-3,
        # which the itzg image's Paper installer rejects as VERSION values.
        values = [value for value in values if re.fullmatch(r"\d+(?:\.\d+){1,2}", str(value))]
    if not values:
        raise DockerError("Không lấy được danh sách phiên bản từ API của nền tảng")
    if key == "NEOFORGE":
        loader_by_mc = {}
        for loader in releases:
            if "craftmine" in str(loader):
                continue
            mc = _neoforge_game_version(loader)
            if not mc:
                continue
            loader_by_mc[mc] = loader
        options = [{"version": v, "loaderVersion": loader_by_mc.get(v, "")} for v in values]
    else:
        options = [{"version": v, "loaderVersion": ""} for v in values]
    VERSION_CACHE[key] = (now, options)
    return options


def _docker(args, timeout=60, check=True, capture=True):
    if not shutil.which("docker"):
        raise DockerError("Docker CLI chưa được cài trên host")
    try:
        result = subprocess.run(["docker", *map(str, args)], capture_output=capture, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DockerError("Docker Engine không phản hồi kịp thời") from exc
    if check and result.returncode:
        message = (result.stderr or result.stdout or "Docker command failed").strip()
        raise DockerError(message[-1200:])
    return result


def _host_command(args, timeout=60):
    try:
        return subprocess.run(list(map(str, args)), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DockerError("Host command timed out") from exc


def _read_index():
    try:
        value = json.loads(SERVER_INDEX.read_text())
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_index(servers):
    DATA.mkdir(parents=True, exist_ok=True)
    temp = SERVER_INDEX.with_suffix(".tmp")
    temp.write_text(json.dumps(servers, indent=2, ensure_ascii=False) + "\n")
    temp.chmod(0o600)
    temp.replace(SERVER_INDEX)


def runtime_status():
    binary = shutil.which("docker")
    result = {"installed": bool(binary), "running": False, "version": "", "error": "", "images": []}
    result["javaEnvironments"] = [
        {"version": version, "image": f"{IMAGE}:{tag}", "installed": False}
        for version, tag in JAVA_IMAGES.items()
    ]
    if not binary:
        result["error"] = "Docker CLI chưa được cài"
        return result
    info = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=12, check=False)
    if info.returncode:
        result["error"] = (info.stderr or "Docker Engine chưa chạy hoặc tài khoản chưa có quyền truy cập").strip()
        return result
    result["running"] = True
    result["version"] = info.stdout.strip()
    images = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=20, check=False)
    if images.returncode == 0:
        result["images"] = [line for line in images.stdout.splitlines() if line.startswith(IMAGE + ":")]
    result["javaEnvironments"] = [
        {"version": version, "image": f"{IMAGE}:{tag}",
         "installed": f"{IMAGE}:{tag}" in result["images"]}
        for version, tag in JAVA_IMAGES.items()
    ]
    return result


def _inspect(container):
    result = _docker(["inspect", "--format", "{{json .State}}", container], check=False)
    if result.returncode:
        return "MISSING", ""
    try:
        state = json.loads(result.stdout)
        status = state.get("Status", "unknown").upper()
        detail = state.get("Error", "")
        if status == "RUNNING":
            return "RUNNING", detail
        if status in {"CREATED", "RESTARTING"}:
            return "STARTING", detail
        if status in {"EXITED", "DEAD"}:
            return "STOPPED", detail
        return status, detail
    except json.JSONDecodeError:
        return "UNKNOWN", ""


def list_servers():
    records = _read_index()
    output = []
    for item in records:
        status, detail = _inspect(item["container"])
        output.append({**item, "status": status, "statusDetail": detail})
    return output


def get_server(server_id):
    for server in list_servers():
        if server["id"] == server_id:
            return server
    return None


def _slug(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:28] or "server"
    return base


def create_server(data):
    name = str(data.get("name", "")).strip()
    motd = str(data.get("motd", "A MineServe Minecraft Server")).strip()
    platform_key = str(data.get("platform", "")).upper()
    version = str(data.get("version", "")).strip()
    loader_version = str(data.get("loaderVersion", "")).strip()
    if not name or len(name) > 48:
        raise ValueError("Tên server cần từ 1 đến 48 ký tự")
    if len(motd) > 255:
        raise ValueError("MOTD tối đa 255 ký tự")
    if platform_key not in PLATFORMS:
        raise ValueError("Nền tảng server không hợp lệ")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}", version):
        raise ValueError("Phiên bản không hợp lệ")
    available_versions = _version_options(platform_key)
    selected_version = next((entry for entry in available_versions if entry["version"] == version), None)
    if selected_version is None:
        raise ValueError(f"Phiên bản {version} không được hỗ trợ bởi {PLATFORMS[platform_key]['name']}")
    if platform_key == "NEOFORGE" and loader_version and selected_version.get("loaderVersion") != loader_version:
        raise ValueError("Loader NeoForge không khớp với phiên bản Minecraft đã chọn")
    init_ram = int(data.get("initRamMb", data.get("ramMb", 2048)))
    max_ram = int(data.get("maxRamMb", data.get("ramMb", 2048)))
    ram = max_ram
    port = int(data.get("port", 25565))
    if not 256 <= init_ram <= 262144 or not 512 <= max_ram <= 262144:
        raise ValueError("RAM phải từ 256 MB đến 256 GB")
    if init_ram > max_ram:
        raise ValueError("RAM tối thiểu không thể lớn hơn RAM tối đa")
    if not 1 <= port <= 65535:
        raise ValueError("Cổng phải nằm trong khoảng 1–65535")
    accepted = data.get("eulaAccepted") is True
    if not accepted:
        raise ValueError("Cần đồng ý EULA để tạo server")
    with DOCKER_LOCK:
        records = _read_index()
        if any(int(item["port"]) == port for item in records):
            raise ValueError(f"Cổng {port} đã được một server khác sử dụng")
        if not runtime_status()["running"]:
            raise DockerError("Docker Engine chưa sẵn sàng. Mở App Settings để kiểm tra runtime.")
        server_id = uuid.uuid4().hex[:12]
        container = f"mineserve-{_slug(name)}-{server_id[:6]}"
        info = PLATFORMS[platform_key]
        java = _recommended_java(version, platform_key)
        image = f"{IMAGE}:{JAVA_IMAGES[java]}"
        map_port = _next_map_port(records)
        server_dir = SERVERS_DIR / server_id
        server_dir.mkdir(parents=True, exist_ok=False)
        (server_dir / "plugins").mkdir(exist_ok=True)
        if platform_key == "PAPER_GEYSER":
            _install_geyser(server_dir)
        rcon_password = uuid.uuid4().hex
        run_args = [
            "run", "-d", "--name", container, "--restart", "unless-stopped", "--stop-timeout", "60",
            "--memory", f"{max_ram + 512}m", "--label", "mineserve.managed=true",
            "--label", f"mineserve.id={server_id}",
            "-p", f"{port}:25565/tcp", "-p", f"{map_port}:8080/tcp", "-e", "EULA=TRUE", "-e", f"TYPE={info['type']}",
            "-e", f"VERSION={version}", "-e", f"INIT_MEMORY={init_ram}M", "-e", f"MAX_MEMORY={max_ram}M", "-e", f"MOTD={motd}",
            "-e", "SERVER_PORT=25565", "-e", "ENABLE_RCON=true", "-e", f"RCON_PASSWORD={rcon_password}",
            "-e", f"UID={os.getuid()}", "-e", f"GID={os.getgid()}",
            "-v", f"{server_dir}:/data", image,
        ]
        if platform_key == "NEOFORGE" and loader_version:
            run_args.extend(["-e", f"NEOFORGE_VERSION={loader_version}"])
        if platform_key == "FORGE" and loader_version:
            run_args.extend(["-e", f"FORGE_VERSION={loader_version}"])
        if platform_key == "FABRIC" and loader_version:
            run_args.extend(["-e", f"FABRIC_LOADER_VERSION={loader_version}"])
        if platform_key == "PAPER_GEYSER":
            bedrock_port = _next_udp_port(records)
            run_args[run_args.index("-p"):run_args.index("-p")] = ["-p", f"{bedrock_port}:19132/udp"]
        try:
            result = _docker(run_args, timeout=300)
        except Exception:
            shutil.rmtree(server_dir, ignore_errors=True)
            raise
        item = {
            "id": server_id, "name": name, "platform": platform_key,
            "platformName": info["name"], "container": container, "version": version,
            "loaderVersion": loader_version, "javaVersion": java, "ramMb": ram,
            "port": port, "mapPort": map_port, "bedrockPort": bedrock_port if platform_key == "PAPER_GEYSER" else None,
            "initRamMb": init_ram, "maxRamMb": max_ram, "jvmFlags": {"aikar": False, "meowice": False, "meowiceGraalvm": False, "flare": False, "simd": False},
            "supportsPlugins": info["plugins"], "supportsMods": info["mods"],
            "motd": motd, "directory": str(server_dir), "image": image,
            "useAikarFlags": False, "jvmOpts": "", "jvmXxOpts": "",
            "rconPassword": rcon_password, "createdAt": int(time.time()),
            "containerId": result.stdout.strip(),
        }
        records.append(item)
        _write_index(records)
        return {**item, "status": "STARTING", "statusDetail": ""}


def _recommended_java(version, platform_key):
    numeric = [int(p) for p in re.findall(r"\d+", version)[:3]]
    if not numeric:
        return PLATFORMS[platform_key]["java"]
    if numeric[0] >= 25 or numeric[0] >= 26 or version.startswith("26"):
        return 25
    if version.startswith("1."):
        minor = numeric[1] if len(numeric) > 1 else 0
        patch = numeric[2] if len(numeric) > 2 else 0
        if minor >= 21 or (minor == 20 and patch >= 5):
            return 21
        if minor >= 17:
            return 17
        return 8
    return 21


def _next_udp_port(records):
    used = {int(item.get("bedrockPort") or 0) for item in records}
    published = set()
    docker_ports = _docker(["ps", "--format", "{{.Ports}}"], timeout=10, check=False)
    if docker_ports.returncode == 0:
        published.update(int(value) for value in re.findall(r":(\d+)->\d+/udp", docker_ports.stdout))
    port = 19132
    while port <= 65535:
        if port not in used and port not in published:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind(("0.0.0.0", port))
                return port
            except OSError:
                pass
            finally:
                probe.close()
        port += 1
    raise DockerError("Không còn cổng UDP trống cho Geyser")


def _next_map_port(records):
    used = {int(item.get("mapPort") or 0) for item in records}
    port = 18080
    while port < 65536:
        if port not in used:
            probe = socket.socket()
            try:
                probe.bind(("0.0.0.0", port))
                return port
            except OSError:
                pass
            finally:
                probe.close()
        port += 1
    raise DockerError("Không còn cổng trống cho web map")


def _install_geyser(server_dir):
    url = "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/spigot"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    target = server_dir / "plugins" / "Geyser-Spigot.jar"
    with urllib.request.urlopen(request, timeout=90) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    config = server_dir / "plugins" / "Geyser-Spigot" / "config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("bedrock:\n  address: 0.0.0.0\n  port: 19132\n  clone-remote-port: false\nremote:\n  address: auto\n  port: 25565\n  auth-type: online\n")


def _require_server(server_id):
    records = _read_index()
    item = next((entry for entry in records if entry["id"] == server_id), None)
    if not item:
        raise ValueError("Không tìm thấy server")
    return records, item


def server_action(server_id, action, purge_data=False):
    records, item = _require_server(server_id)
    if action == "start":
        _docker(["start", item["container"]])
    elif action == "stop":
        _docker(["stop", "--time", "60", item["container"]], timeout=75)
    elif action == "restart":
        _docker(["restart", "--time", "60", item["container"]], timeout=75)
    elif action == "delete":
        _docker(["rm", "-f", item["container"]], timeout=60, check=False)
        if item.get("portForward", {}).get("enabled"):
            try:
                disable_port_forward(server_id)
            except Exception:
                pass
        records.remove(item)
        _write_index(records)
        if purge_data:
            shutil.rmtree(item["directory"], ignore_errors=True)
        return {"deleted": True}
    else:
        raise ValueError("Thao tác không hợp lệ")
    status, detail = _inspect(item["container"])
    return {"id": server_id, "status": status, "statusDetail": detail}


def update_server_resources(server_id, changes):
    records, item = _require_server(server_id)
    max_ram = int(changes.get("maxRamMb", changes.get("ramMb", item.get("maxRamMb", item["ramMb"]))))
    init_ram = int(changes.get("initRamMb", item.get("initRamMb", item["ramMb"])))
    ram = max_ram
    port = int(changes.get("port", item["port"]))
    motd = str(changes.get("motd", item["motd"])).strip()
    jvm_opts = str(changes.get("jvmOpts", item.get("jvmOpts", ""))).strip()
    jvm_xx_opts = str(changes.get("jvmXxOpts", item.get("jvmXxOpts", ""))).strip()
    old_flags = item.get("jvmFlags", {})
    flags_input = changes.get("jvmFlags", old_flags)
    jvm_flags = {key: bool(flags_input.get(key, old_flags.get(key, False))) for key in ("aikar", "meowice", "meowiceGraalvm", "flare", "simd")}
    if "jvmFlags" not in changes:
        jvm_flags["aikar"] = bool(changes.get("useAikarFlags", item.get("useAikarFlags", jvm_flags["aikar"])))
    if jvm_flags["aikar"] and jvm_flags["meowice"]:
        raise ValueError("Chọn Aikar hoặc MeowIce, không thể bật cả hai cùng lúc")
    if not 256 <= init_ram <= 262144 or not 512 <= ram <= 262144:
        raise ValueError("RAM phải từ 256 MB đến 256 GB")
    if init_ram > ram:
        raise ValueError("RAM tối thiểu không thể lớn hơn RAM tối đa")
    if not 1 <= port <= 65535:
        raise ValueError("Port phải từ 1 đến 65535")
    if any(x["id"] != server_id and int(x["port"]) == port for x in records):
        raise ValueError(f"Port {port} đã được server khác sử dụng")
    if len(motd) > 255 or "\n" in motd or "\r" in motd:
        raise ValueError("MOTD không hợp lệ")
    if len(jvm_opts) > 2048 or len(jvm_xx_opts) > 2048 or any(ord(ch) < 32 and ch not in "\t" for ch in jvm_opts + jvm_xx_opts):
        raise ValueError("JVM options quá dài hoặc chứa ký tự điều khiển")
    running = _inspect(item["container"])[0] == "RUNNING"
    old = dict(item)
    refresh_forward = int(item["port"]) != port and item.get("portForward", {}).get("enabled", False)
    if refresh_forward:
        try:
            disable_port_forward(server_id)
            item["portForward"] = {"enabled": False, "externalIp": "", "externalPort": item["port"]}
        except Exception:
            pass
    if running:
        _docker(["stop", "--time", "60", item["container"]], timeout=75)
    _docker(["rm", "-f", item["container"]], check=False)
    item.update(ramMb=ram, initRamMb=init_ram, maxRamMb=ram, port=port, motd=motd,
                jvmOpts=jvm_opts, jvmXxOpts=jvm_xx_opts, jvmFlags=jvm_flags,
                useAikarFlags=jvm_flags["aikar"])
    if not item.get("mapPort"):
        item["mapPort"] = _next_map_port(records)
    try:
        _run_managed_container(item)
    except Exception:
        item.update(old)
        try:
            _run_managed_container(item)
        except Exception:
            pass
        if refresh_forward:
            try:
                enable_port_forward(server_id)
            except Exception:
                pass
        raise
    _write_index(records)
    forward_error = ""
    if refresh_forward:
        try:
            enable_port_forward(server_id)
        except Exception as exc:
            forward_error = str(exc)
    return {"updated": True, "ramMb": ram, "port": port, "motd": motd, "restartRequired": True,
            "portForwardError": forward_error}


def update_server_version(server_id, version, loader_version=""):
    records, item = _require_server(server_id)
    version = str(version).strip()
    if not any(entry["version"] == version for entry in _version_options(item["platform"])):
        raise ValueError("Phiên bản không có trong danh sách platform")
    if version == item["version"] and loader_version == item.get("loaderVersion", ""):
        return {"updated": False, "message": "Server đang ở phiên bản này"}
    if Path(item["directory"]).exists() and any(Path(item["directory"]).iterdir()):
        create_backup(server_id, world_only=False)
    was_running = _inspect(item["container"])[0] == "RUNNING"
    if was_running:
        _docker(["stop", "--time", "60", item["container"]], timeout=75)
    _docker(["rm", "-f", item["container"]], check=False)
    old = dict(item)
    item["version"] = version
    item["loaderVersion"] = str(loader_version)
    item["javaVersion"] = _recommended_java(version, item["platform"])
    item["image"] = f"{IMAGE}:{JAVA_IMAGES[item['javaVersion']]}"
    try:
        _run_managed_container(item)
        _write_index(records)
    except Exception:
        item.update(old)
        try:
            _run_managed_container(item)
        except Exception:
            pass
        _write_index(records)
        raise
    return {"updated": True, "version": version, "javaVersion": item["javaVersion"], "wasRunning": was_running}


def update_latest_build(server_id):
    _, item = _require_server(server_id)
    if item["platform"] not in {"PAPER", "PAPER_GEYSER", "PURPUR", "FOLIA"}:
        raise ValueError("Upstream build update chỉ hỗ trợ Paper, Purpur và Folia")
    if _inspect(item["container"])[0] == "RUNNING":
        _docker(["stop", "--time", "60", item["container"]], timeout=75)
    if any(Path(item["directory"]).iterdir()):
        create_backup(server_id, world_only=False)
    (Path(item["directory"]) / "server.jar").unlink(missing_ok=True)
    records, current = _require_server(server_id)
    _docker(["rm", "-f", current["container"]], check=False)
    _run_managed_container(current)
    _write_index(records)
    return {"updated": True, "version": item["version"], "message": "Container khởi động lại để tải build mới nhất"}


def _run_managed_container(item):
    if not item.get("mapPort"):
        records = [entry for entry in _read_index() if entry.get("id") != item.get("id")]
        item["mapPort"] = _next_map_port(records)
    args = ["run", "-d", "--name", item["container"], "--restart", "unless-stopped", "--stop-timeout", "60",
            "--memory", f"{int(item.get('maxRamMb', item['ramMb'])) + 512}m", "--label", "mineserve.managed=true",
            "--label", f"mineserve.id={item['id']}", "-p", f"{item['port']}:25565/tcp",
            "-p", f"{item.get('mapPort', 18080)}:8080/tcp"]
    if item.get("bedrockPort"):
        args.extend(["-p", f"{item['bedrockPort']}:19132/udp"])
    args.extend(["-e", "EULA=TRUE", "-e", f"TYPE={PLATFORMS[item['platform']]['type']}",
                 "-e", f"VERSION={item['version']}", "-e", f"INIT_MEMORY={item.get('initRamMb', item['ramMb'])}M",
                 "-e", f"MAX_MEMORY={item.get('maxRamMb', item['ramMb'])}M", "-e", f"MOTD={item['motd']}",
                 "-e", "SERVER_PORT=25565", "-e", "ENABLE_RCON=true", "-e", f"RCON_PASSWORD={item['rconPassword']}",
                 "-e", f"UID={os.getuid()}", "-e", f"GID={os.getgid()}"])
    flags = item.get("jvmFlags", {})
    args.extend(["-e", f"USE_AIKAR_FLAGS={'TRUE' if flags.get('aikar', item.get('useAikarFlags')) else 'FALSE'}",
                 "-e", f"USE_MEOWICE_FLAGS={'TRUE' if flags.get('meowice') else 'FALSE'}",
                 "-e", f"USE_MEOWICE_GRAALVM_FLAGS={'TRUE' if flags.get('meowiceGraalvm') else 'FALSE'}",
                 "-e", f"USE_FLARE_FLAGS={'TRUE' if flags.get('flare') else 'FALSE'}",
                 "-e", f"USE_SIMD_FLAGS={'TRUE' if flags.get('simd') else 'FALSE'}",
                 "-e", f"JVM_OPTS={item.get('jvmOpts', '')}", "-e", f"JVM_XX_OPTS={item.get('jvmXxOpts', '')}"])
    if item["platform"] == "NEOFORGE" and item.get("loaderVersion"):
        args.extend(["-e", f"NEOFORGE_VERSION={item['loaderVersion']}"])
    if item["platform"] == "FORGE" and item.get("loaderVersion"):
        args.extend(["-e", f"FORGE_VERSION={item['loaderVersion']}"])
    if item["platform"] == "FABRIC" and item.get("loaderVersion"):
        args.extend(["-e", f"FABRIC_LOADER_VERSION={item['loaderVersion']}"])
    args.extend(["-v", f"{item['directory']}:/data", item["image"]])
    result = _docker(args, timeout=300)
    item["containerId"] = result.stdout.strip()


def server_logs(server_id, tail=300):
    _, item = _require_server(server_id)
    args = ["logs", item["container"]] if str(tail).lower() == "all" else ["logs", "--tail", str(max(1, min(int(tail), 2000))), item["container"]]
    result = _docker(args, check=False)
    return {"lines": (result.stdout + result.stderr).splitlines()}


def analyze_server_crash(server_id):
    lines = server_logs(server_id, 2000)["lines"]
    text = "\n".join(lines).lower()
    findings = []
    checks = [
        (("outofmemoryerror", "java heap space", "cannot reserve enough space"), "Out of memory", "Increase RAM allocation or reduce view-distance and simulation-distance."),
        (("unsupportedclassversionerror", "class file version", "requires java"), "Java version mismatch", "Choose a Minecraft version compatible with its Java environment, then recreate the container."),
        (("eula", "you need to agree to the eula"), "EULA setup", "Accept the Minecraft EULA before starting the server."),
        (("address already in use", "failed to bind to port"), "Port conflict", "Assign an unused host port in Server resources."),
        (("duplicate mod", "mod resolution encountered an incompatible mod set", "requires fabric loader"), "Mod or loader conflict", "Remove the conflicting mod or install the loader version required by the modpack."),
    ]
    for patterns, title, advice in checks:
        if any(pattern in text for pattern in patterns):
            findings.append({"title": title, "advice": advice})
    return {"findings": findings, "summary": "Likely causes found" if findings else "No known crash signature found in recent logs",
            "logLines": lines[-80:]}


def send_command(server_id, command):
    _, item = _require_server(server_id)
    command = str(command).strip()
    if not command or len(command) > 2000 or "\n" in command or "\r" in command:
        raise ValueError("Lệnh không hợp lệ")
    result = _docker(["exec", item["container"], "rcon-cli", command], timeout=15, check=False)
    if result.returncode:
        raise DockerError((result.stderr or result.stdout).strip() or "Không gửi được lệnh RCON")
    return {"output": result.stdout.strip()}


def server_files(server_id, relative=""):
    _, item = _require_server(server_id)
    root = Path(item["directory"]).resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Đường dẫn không hợp lệ")
    return root, target


def list_files(server_id, relative=""):
    root, target = server_files(server_id, relative)
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            size = path.stat().st_size
            is_dir = path.is_dir()
        except OSError:
            continue
        entries.append({"name": path.name, "path": str(path.relative_to(root)), "isDirectory": is_dir,
                        "sizeBytes": size, "editable": not is_dir and size <= 1024 * 1024 and path.suffix.lower() in
                        {".txt", ".properties", ".yml", ".yaml", ".json", ".toml", ".cfg", ".conf", ".md"}})
    return {"path": relative, "files": entries}


def read_file(server_id, relative):
    _, path = server_files(server_id, relative)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Tệp lớn hơn 1 MB")
    return path.read_text(encoding="utf-8")


def write_file(server_id, relative, content):
    root, path = server_files(server_id, relative)
    if path == root or path.name in {"eula.txt", "server.jar"}:
        raise ValueError("Không cho phép ghi tệp này")
    if len(content.encode()) > 1024 * 1024:
        raise ValueError("Tệp lớn hơn 1 MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_binary(server_id, relative, content):
    root, path = server_files(server_id, relative)
    if path == root or path.name in {"eula.txt", "server.jar", "ops.json", "whitelist.json"}:
        raise ValueError("Không cho phép ghi tệp hệ thống hoặc phân quyền này")
    if len(content) > 200 * 1024 * 1024:
        raise ValueError("Tệp lớn hơn giới hạn 200 MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_binary_stream(server_id, relative, source, length, max_bytes=200 * 1024 * 1024):
    root, path = server_files(server_id, relative)
    if path == root or path.name in {"eula.txt", "server.jar", "ops.json", "whitelist.json"}:
        raise ValueError("Không cho phép ghi tệp hệ thống hoặc phân quyền này")
    if length <= 0 or length > max_bytes:
        raise ValueError(f"Tệp phải có kích thước từ 1 byte đến {max_bytes // (1024 * 1024)} MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".uploading")
    remaining = length
    try:
        with temp.open("wb") as output:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Tải tệp chưa hoàn tất")
                output.write(chunk)
                remaining -= len(chunk)
        temp.replace(path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return path


def delete_file(server_id, relative):
    root, path = server_files(server_id, relative)
    if path == root or path.is_dir():
        raise ValueError("Không thể xóa thư mục gốc hoặc thư mục con")
    path.unlink()


def create_server_file(server_id, relative, is_directory=False, content=""):
    root, path = server_files(server_id, relative)
    if path == root or path.exists():
        raise ValueError("Đường dẫn đã tồn tại hoặc không hợp lệ")
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_directory:
        path.mkdir()
    else:
        if len(str(content).encode()) > 1024 * 1024:
            raise ValueError("Tệp mới tối đa 1 MB")
        path.write_text(str(content), encoding="utf-8")
    return {"created": True}


def rename_server_file(server_id, relative, new_name):
    root, path = server_files(server_id, relative)
    if path == root or not path.exists() or "/" in new_name or "\\" in new_name or new_name in {".", ".."}:
        raise ValueError("Tên tệp mới không hợp lệ")
    target = (path.parent / new_name).resolve()
    if target.exists() or root not in target.parents:
        raise ValueError("Tên đích đã tồn tại hoặc không hợp lệ")
    path.rename(target)
    return {"renamed": True}


def duplicate_server_file(server_id, relative):
    root, source = server_files(server_id, relative)
    if source == root or not source.exists():
        raise ValueError("Không tìm thấy tệp")
    target = source.with_name(source.name + ".copy")
    index = 2
    while target.exists():
        target = source.with_name(f"{source.name}.copy{index}")
        index += 1
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    return {"path": str(target.relative_to(root))}


def unzip_server_file(server_id, relative, destination=""):
    root, archive = server_files(server_id, relative)
    if not archive.is_file() or archive.suffix.lower() != ".zip":
        raise ValueError("Chọn tệp .zip")
    target = (root / destination).resolve() if destination else archive.parent
    if target != root and root not in target.parents:
        raise ValueError("Thư mục giải nén không hợp lệ")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        total = sum(item.file_size for item in members)
        if total > 2 * 1024 * 1024 * 1024:
            raise ValueError("ZIP giải nén vượt giới hạn 2 GB")
        for member in members:
            candidate = (target / member.filename.replace("\\", "/")).resolve()
            if candidate != target and target not in candidate.parents:
                raise ValueError("ZIP chứa đường dẫn không an toàn")
        source.extractall(target)
    return {"extracted": len(members)}


def search_server_files(server_id, query):
    root, _ = server_files(server_id)
    needle = str(query).strip().lower()
    if not needle:
        return {"results": []}
    results = []
    for path in root.rglob("*"):
        if len(results) >= 200:
            break
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = str(path.relative_to(root))
            if needle in path.name.lower():
                results.append({"path": relative, "match": "filename"})
            elif path.stat().st_size <= 256 * 1024 and path.suffix.lower() in {".txt", ".yml", ".yaml", ".json", ".properties", ".toml", ".conf"}:
                if needle in path.read_text(encoding="utf-8", errors="ignore").lower():
                    results.append({"path": relative, "match": "content"})
        except OSError:
            continue
    return {"results": results}


def create_backup(server_id, world_only=False):
    _, item = _require_server(server_id)
    directory = Path(item["directory"])
    backup_dir = DATA / "backups" / server_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = "world" if world_only else "server"
    archive = backup_dir / f"{label}-{stamp}.zip"
    source_paths = [directory / name for name in ("world", "world_nether", "world_the_end") if (directory / name).exists()] if world_only else [directory]
    if not source_paths:
        raise ValueError("Chưa có world để sao lưu")
    state, _ = _inspect(item["container"])
    was_running = state == "RUNNING"
    if was_running:
        _docker(["stop", "--time", "60", item["container"]], timeout=75)
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=2) as target:
            for source in source_paths:
                if source.is_dir():
                    for file in source.rglob("*"):
                        if file.is_file() and not file.is_symlink():
                            relative = file.relative_to(directory) if world_only else file.relative_to(directory)
                            target.write(file, relative.as_posix())
    finally:
        if was_running:
            _docker(["start", item["container"]], timeout=60, check=False)
    result = {"name": archive.name, "sizeBytes": archive.stat().st_size, "worldOnly": world_only}
    if item.get("cloudBackup", {}).get("enabled") and item.get("cloudBackup", {}).get("autoSync"):
        result["cloudSync"] = sync_backup_webdav(server_id, archive.name)
    return result


def list_backups(server_id):
    _require_server(server_id)
    backup_dir = DATA / "backups" / server_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    return [{"name": path.name, "sizeBytes": path.stat().st_size,
             "modifiedAt": int(path.stat().st_mtime)}
            for path in sorted(backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)]


def delete_backup(server_id, filename):
    _require_server(server_id)
    if not re.fullmatch(r"(?:world|server)-\d{8}-\d{6}\.zip", filename):
        raise ValueError("Tên backup không hợp lệ")
    target = DATA / "backups" / server_id / filename
    if not target.is_file():
        raise ValueError("Không tìm thấy backup")
    target.unlink()
    return {"deleted": True}


def save_cloud_config(server_id, config):
    records, item = _require_server(server_id)
    url = str(config.get("url", "")).strip()
    provider = str(config.get("provider", "webdav")).lower()
    local_path = str(config.get("localPath", "")).strip()
    if provider not in {"webdav", "local"}:
        raise ValueError("Cloud backup provider không hợp lệ")
    if url and not url.startswith(("https://", "http://")):
        raise ValueError("WebDAV URL phải bắt đầu bằng http:// hoặc https://")
    password = str(config.get("password", "")) or item.get("cloudBackup", {}).get("password", "")
    if config.get("enabled") and provider == "webdav" and (not url or not str(config.get("username", "")) or not password):
        raise ValueError("WebDAV cần URL, username và password/app token")
    if config.get("enabled") and provider == "local" and not local_path:
        raise ValueError("Nhập thư mục local hoặc thư mục cloud đã mount")
    remote_parts = str(config.get("remotePath", "/MineServe/Backups")).replace("\\", "/").strip("/").split("/")
    if any(part in {".", ".."} for part in remote_parts):
        raise ValueError("WebDAV remote path không hợp lệ")
    item["cloudBackup"] = {"enabled": bool(config.get("enabled", False)), "provider": provider,
                           "localPath": local_path, "url": url.rstrip("/"),
                           "username": str(config.get("username", ""))[:256],
                           "password": password[:512],
                           "remotePath": "/" + "/".join(part for part in remote_parts if part),
                           "autoSync": bool(config.get("autoSync", False))}
    _write_index(records)
    return {key: value for key, value in item["cloudBackup"].items() if key != "password"} | {"hasPassword": bool(item["cloudBackup"]["password"])}


def get_cloud_config(server_id):
    _, item = _require_server(server_id)
    config = item.get("cloudBackup", {})
    return {key: value for key, value in config.items() if key != "password"} | {"hasPassword": bool(config.get("password"))}


def sync_backup_webdav(server_id, filename):
    _, item = _require_server(server_id)
    config = item.get("cloudBackup", {})
    if not config.get("enabled"):
        raise ValueError("Chưa cấu hình WebDAV")
    if not re.fullmatch(r"(?:world|server)-\d{8}-\d{6}\.zip", filename):
        raise ValueError("Tên backup không hợp lệ")
    archive = DATA / "backups" / server_id / filename
    if not archive.is_file():
        raise ValueError("Không tìm thấy backup")
    if config.get("provider", "webdav") == "local":
        target_dir = Path(config.get("localPath", "")).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        shutil.copy2(archive, target)
        return {"synced": True, "path": str(target)}
    from urllib.parse import quote
    base = config["url"].rstrip("/")
    remote_path = "/".join(quote(part) for part in config.get("remotePath", "/MineServe/Backups").strip("/").split("/"))
    target = f"{base}/{remote_path}/{quote(filename)}"
    credentials = (str(config.get("username", "")) + ":" + str(config.get("password", ""))).encode()
    import base64
    headers = {"Authorization": "Basic " + base64.b64encode(credentials).decode(), "User-Agent": UA}
    collection = base
    for part in config.get("remotePath", "/MineServe/Backups").strip("/").split("/"):
        if not part:
            continue
        collection += "/" + quote(part)
        request = urllib.request.Request(collection, headers=headers, method="MKCOL")
        try:
            with urllib.request.urlopen(request, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code not in {405, 301, 302}:
                raise
    parsed = urllib.parse.urlsplit(target)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=1800)
    try:
        connection.putrequest("PUT", parsed.path)
        connection.putheader("Authorization", headers["Authorization"])
        connection.putheader("User-Agent", UA)
        connection.putheader("Content-Type", "application/zip")
        connection.putheader("Content-Length", str(archive.stat().st_size))
        connection.endheaders()
        with archive.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                connection.send(chunk)
        response = connection.getresponse()
        if response.status not in {200, 201, 204}:
            raise DockerError(f"WebDAV trả về HTTP {response.status}")
    finally:
        connection.close()
    return {"synced": True, "url": target}


def restore_backup(server_id, filename):
    _, item = _require_server(server_id)
    if not re.fullmatch(r"(?:world|server)-\d{8}-\d{6}\.zip", filename):
        raise ValueError("Tên backup không hợp lệ")
    archive = DATA / "backups" / server_id / filename
    if not archive.is_file():
        raise ValueError("Không tìm thấy backup")
    if _inspect(item["container"])[0] == "RUNNING":
        raise ValueError("Dừng server trước khi khôi phục backup")
    # Snapshot current state before replacing files.
    create_backup(server_id, world_only=filename.startswith("world-"))
    root = Path(item["directory"]).resolve()
    world_only = filename.startswith("world-")
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        for entry in entries:
            candidate = (root / entry.filename).resolve()
            if candidate != root and root not in candidate.parents:
                raise ValueError("Backup có đường dẫn không an toàn")
        bad = source.testzip()
        if bad:
            raise ValueError(f"Backup bị lỗi ở entry {bad}")
        legacy_prefix = f"{server_id}/" if not world_only and entries and all(
            entry.filename.startswith(f"{server_id}/") or entry.is_dir() and entry.filename.rstrip("/") == server_id
            for entry in entries) else ""
        if world_only:
            for name in ("world", "world_nether", "world_the_end"):
                shutil.rmtree(root / name, ignore_errors=True)
        else:
            for path in root.iterdir():
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.is_file():
                    path.unlink()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            if legacy_prefix:
                name = name.removeprefix(legacy_prefix)
            if not name:
                continue
            target = (root / name).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Backup có đường dẫn không an toàn")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file)
    return {"restored": True}


def read_properties(server_id):
    _, item = _require_server(server_id)
    path = Path(item["directory"]) / "server.properties"
    properties = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "!")) and "=" in line:
                key, value = line.split("=", 1)
                properties[key.strip()] = value.strip()
    return {"properties": properties}


def write_properties(server_id, properties):
    _, item = _require_server(server_id)
    if not isinstance(properties, dict) or len(properties) > 256:
        raise ValueError("Cấu hình server.properties không hợp lệ")
    clean = {}
    for key, value in properties.items():
        key, value = str(key).strip(), str(value).replace("\r", "").replace("\n", "\\n")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key) or len(value) > 4096:
            raise ValueError("Tên hoặc giá trị thuộc tính không hợp lệ")
        clean[key] = value
    path = Path(item["directory"]) / "server.properties"
    path.write_text("# MineServe server properties\n" + "".join(f"{k}={v}\n" for k, v in sorted(clean.items())), encoding="utf-8")
    return {"saved": True, "restartRequired": True}


def player_lists(server_id):
    _, item = _require_server(server_id)
    root = Path(item["directory"])
    result = {}
    for key, filename in (("whitelist", "whitelist.json"), ("ops", "ops.json"),
                          ("bannedPlayers", "banned-players.json"), ("bannedIps", "banned-ips.json")):
        try:
            content = json.loads((root / filename).read_text(encoding="utf-8"))
            result[key] = content if isinstance(content, list) else []
        except (OSError, json.JSONDecodeError):
            result[key] = []
    return result


def modrinth_search(server_id, query, project_type=None):
    _, item = _require_server(server_id)
    if not item.get("supportsPlugins") and not item.get("supportsMods"):
        return {"hits": []}
    params = {"query": str(query)[:100], "limit": "25"}
    facets = [["project_type:plugin"]] if item.get("supportsPlugins") else [["project_type:mod"]]
    if project_type in {"plugin", "mod"}:
        facets = [[f"project_type:{project_type}"]]
    if item.get("platform") == "FABRIC":
        facets.append(["categories:fabric"])
    elif item.get("platform") in {"FORGE", "NEOFORGE"}:
        facets.append(["categories:forge", "categories:neoforge"])
    params["facets"] = json.dumps(facets, separators=(",", ":"))
    url = "https://api.modrinth.com/v2/search?" + urllib.parse.urlencode(params)
    data = _request_json(url)
    return {"hits": data.get("hits", [])}


def install_modrinth_project(server_id, project_id):
    _, item = _require_server(server_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", project_id):
        raise ValueError("Modrinth project ID không hợp lệ")
    project_type = "plugin" if item.get("supportsPlugins") else "mod"
    loaders = {"PAPER": ["paper", "spigot", "bukkit"], "PAPER_GEYSER": ["paper", "spigot", "bukkit"], "PURPUR": ["paper", "purpur", "spigot"],
               "FOLIA": ["paper", "folia"], "FABRIC": ["fabric"], "FORGE": ["forge"], "NEOFORGE": ["neoforge"]}.get(item["platform"], [])
    params = urllib.parse.urlencode({"game_versions": json.dumps([item["version"]]), "loaders": json.dumps(loaders)})
    versions = _request_json(f"https://api.modrinth.com/v2/project/{project_id}/version?{params}")
    if not versions:
        raise ValueError("Không có phiên bản tương thích với Minecraft/platform này")
    files = versions[0].get("files", [])
    selected = next((f for f in files if f.get("primary")), files[0] if files else None)
    if not selected or not selected.get("url"):
        raise ValueError("Modrinth không trả về tệp tải xuống")
    filename = Path(urllib.parse.urlsplit(selected["url"]).path).name
    if not filename.lower().endswith(".jar"):
        raise ValueError("Tệp tải về không phải JAR server")
    folder = "plugins" if project_type == "plugin" else "mods"
    target = Path(item["directory"]) / folder / filename
    request = urllib.request.Request(selected["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=90) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)
    return {"installed": filename, "folder": folder}


def install_modrinth_pack(server_id, relative):
    _, item = _require_server(server_id)
    if _inspect(item["container"])[0] == "RUNNING":
        raise ValueError("Dừng server trước khi cài modpack")
    _, archive_path = server_files(server_id, relative)
    if archive_path.suffix.lower() != ".mrpack" or not archive_path.is_file():
        raise ValueError("Chọn file Modrinth .mrpack đã upload")
    with zipfile.ZipFile(archive_path) as archive:
        try:
            index = json.loads(archive.read("modrinth.index.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Modrinth index không hợp lệ") from exc
        dependencies = index.get("dependencies", {})
        minecraft = str(dependencies.get("minecraft", ""))
        if minecraft and minecraft != item["version"]:
            raise ValueError(f"Modpack yêu cầu Minecraft {minecraft}; server hiện tại là {item['version']}")
        required_loader = next((key for key in ("fabric-loader", "forge", "neoforge", "quilt-loader") if dependencies.get(key)), "")
        if required_loader and required_loader.upper().split("-")[0] not in {item["platform"], "FORGE" if item["platform"] == "NEOFORGE" else item["platform"]}:
            raise ValueError(f"Modpack cần loader {required_loader}; platform server không khớp")
        existing_files = list(Path(item["directory"]).iterdir())
        if existing_files:
            create_backup(server_id, world_only=False)
        root = Path(item["directory"]).resolve()
        # The archive can contain server overrides plus files that must be fetched from Modrinth.
        extracted_bytes = 0
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            if member.is_dir() or not (name.startswith("overrides/") or name.startswith("server-overrides/")):
                continue
            rel = name.split("/", 1)[1]
            target = (root / rel).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Modpack override có đường dẫn không an toàn")
            extracted_bytes += member.file_size
            if extracted_bytes > 1024 * 1024 * 1024:
                raise ValueError("Modpack overrides vượt 1 GB")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        downloaded = 0
        total_bytes = 0
        for entry in index.get("files", []):
            if entry.get("env", {}).get("server") == "unsupported":
                continue
            path = str(entry.get("path", "")).replace("\\", "/")
            target = (root / path).resolve()
            if target != root and root not in target.parents:
                raise ValueError("Modpack file path không an toàn")
            if not path or not entry.get("downloads"):
                continue
            expected_size = int(entry.get("fileSize", 0))
            if expected_size > 300 * 1024 * 1024 or total_bytes + expected_size > 2 * 1024 * 1024 * 1024:
                raise ValueError("Modpack download vượt giới hạn 2 GB")
            request = urllib.request.Request(entry["downloads"][0], headers={"User-Agent": UA})
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha1()
            size = 0
            with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 300 * 1024 * 1024:
                        raise ValueError("Modpack chứa file quá lớn")
                    digest.update(chunk)
                    output.write(chunk)
            expected_hash = entry.get("hashes", {}).get("sha1")
            if expected_hash and digest.hexdigest().lower() != expected_hash.lower():
                target.unlink(missing_ok=True)
                raise ValueError(f"SHA-1 không khớp: {path}")
            total_bytes += size
            downloaded += 1
    if required_loader:
        records, current = _require_server(server_id)
        current["loaderVersion"] = str(dependencies.get(required_loader, ""))
        _run_managed_container(current)
        _write_index(records)
    return {"installedFiles": downloaded, "minecraft": minecraft, "loader": required_loader}


def world_summary(server_id):
    _, item = _require_server(server_id)
    root = Path(item["directory"])
    result = []
    for name in ("world", "world_nether", "world_the_end"):
        path = root / name
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0
        regions = list(path.rglob("*.mca")) if path.is_dir() else []
        result.append({"name": name, "exists": path.is_dir(), "sizeBytes": size, "regionFiles": len(regions)})
    plugin_dir = root / "plugins"
    installed = plugin_dir.is_dir() and any("squaremap" in f.name.lower() for f in plugin_dir.iterdir())
    return {"worlds": result, "squaremapInstalled": installed, "mapPort": item.get("mapPort"),
            "mapUrl": f"http://<host>:{item.get('mapPort')}" if item.get("mapPort") else None}


def _storage_details(item):
    root = Path(item["directory"])
    now = time.time()
    cached = STORAGE_CACHE.get(item["id"])
    if cached and now - cached[0] < 30:
        return cached[1]
    total_bytes = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            try:
                if not path.is_symlink():
                    total_bytes += path.stat().st_size
            except OSError:
                pass
    try:
        usage = shutil.disk_usage(root)
        disk_total, disk_free = usage.total, usage.free
    except OSError:
        disk_total = disk_free = 0
    try:
        contents = sorted(path.name + ("/" if path.is_dir() else "") for path in root.iterdir())[:24]
    except OSError:
        contents = []
    jars = sorted(path.name for path in root.glob("*.jar") if path.is_file())
    primary_jar = "server.jar" if (root / "server.jar").is_file() else (jars[0] if jars else "Not downloaded")
    details = {"totalServerBytes": total_bytes, "hostDiskTotalBytes": disk_total,
               "hostDiskFreeBytes": disk_free, "storageContents": contents,
               "primaryJar": primary_jar, "containerPath": f"{root} → /data"}
    STORAGE_CACHE[item["id"]] = (now, details)
    return details


def server_metrics(server_id):
    _, item = _require_server(server_id)
    stats = _docker(["stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}", item["container"]], check=False, timeout=20)
    cpu, memory, mem_pct = "0%", "0B / 0B", "0%"
    if stats.returncode == 0 and stats.stdout.strip():
        bits = stats.stdout.strip().split("|")
        if len(bits) >= 3:
            cpu, memory, mem_pct = bits[:3]
    try:
        cpu_pct = float(re.search(r"[0-9.]+", cpu).group()) if re.search(r"[0-9.]+", cpu) else 0.0
        memory_pct = float(re.search(r"[0-9.]+", mem_pct).group()) if re.search(r"[0-9.]+", mem_pct) else 0.0
    except ValueError:
        cpu_pct = memory_pct = 0.0
    tps = ""
    mspt = ""
    try:
        output = send_command(server_id, "tps")["output"]
        match = re.search(r"(?:overall|1m|average)[^0-9]*([0-9]+(?:\.[0-9]+)?)", output, re.I)
        if match:
            tps = match.group(1)
    except Exception:
        pass
    try:
        output = send_command(server_id, "tick query")["output"]
        match = re.search(r"(?:average tick time(?: is|:)?)\s*([0-9]+(?:\.[0-9]+)?)\s*ms", output, re.I)
        if match:
            mspt = match.group(1)
    except Exception:
        pass
    status, _ = _inspect(item["container"])
    state_result = _docker(["inspect", "--format", "{{json .State}}", item["container"]], check=False)
    state = {}
    if state_result.returncode == 0:
        try:
            state = json.loads(state_result.stdout)
        except json.JSONDecodeError:
            pass
    try:
        engine = _docker(["info", "--format", "{{.ServerVersion}}"], check=False, timeout=8).stdout.strip()
    except Exception:
        engine = ""
    try:
        log_result = _docker(["logs", "--tail", "100", item["container"]], check=False, timeout=10)
        recent_logs = log_result.stdout + log_result.stderr
        lag_warnings = len(re.findall(r"can't keep up|server overloaded", recent_logs, re.I))
    except Exception:
        lag_warnings = 0
    tps_num = float(tps) if tps else None
    mspt_num = float(mspt) if mspt else None
    if status != "RUNNING":
        loop_state = "Stopped"
    elif tps_num is None or mspt_num is None:
        loop_state = "Unknown"
    elif tps_num >= 19.5 and mspt_num < 50:
        loop_state = "Smooth"
    elif tps_num >= 15 and mspt_num < 100:
        loop_state = "Slight delay"
    else:
        loop_state = "Overloaded"
    if cpu_pct >= 80:
        cpu_diagnostic = "High CPU load; inspect entities, chunks, plugins, and JVM flags."
    elif memory_pct >= 85:
        cpu_diagnostic = "High container memory pressure; review heap and non-heap headroom."
    elif loop_state == "Overloaded":
        cpu_diagnostic = "Game loop is behind its tick budget."
    elif loop_state == "Unknown" and status == "RUNNING":
        cpu_diagnostic = "TPS/MSPT telemetry is unavailable; check server version and RCON."
    elif status == "RUNNING":
        cpu_diagnostic = "No current CPU or game-loop pressure detected."
    else:
        cpu_diagnostic = "Start the container to collect process telemetry."
    history = METRICS_HISTORY.setdefault(server_id, {"tps": [], "mspt": [], "cpu": []})
    if tps_num is not None:
        history["tps"].append(round(tps_num, 2))
    if mspt_num is not None:
        history["mspt"].append(round(mspt_num, 2))
    history["cpu"].append(round(cpu_pct, 2))
    for values in history.values():
        del values[:-60]
    flags = item.get("jvmFlags", {})
    gc_preset = ("MeowIce" if flags.get("meowice") else "Aikar optimized G1GC" if flags.get("aikar", item.get("useAikarFlags")) else "G1GC defaults")
    storage = _storage_details(item)
    return {"cpu": cpu, "cpuPercent": cpu_pct, "cpuThreads": os.cpu_count() or 1,
            "memory": memory, "memoryPercent": mem_pct, "memoryPercentValue": memory_pct,
            "tps": tps, "mspt": mspt, "tpsValue": tps_num, "msptValue": mspt_num,
            "tpsAvailable": tps_num is not None, "msptAvailable": mspt_num is not None,
            "tpsHistory": history["tps"], "msptHistory": history["mspt"], "cpuHistory": history["cpu"],
            "status": status, "engineVersion": engine, "containerPid": state.get("Pid", 0),
            "uptimeSince": state.get("StartedAt", ""), "gameLoopState": loop_state,
            "lagWarnings": lag_warnings, "cpuDiagnostic": cpu_diagnostic,
            "initRamMb": item.get("initRamMb", item["ramMb"]), "maxRamMb": item.get("maxRamMb", item["ramMb"]),
            "javaVersion": item.get("javaVersion", ""), "gcPreset": gc_preset, "jvmFlags": flags,
            "sampledAt": int(time.time()), **storage}


def port_forward_status(server_id):
    _, item = _require_server(server_id)
    return item.get("portForward", {"enabled": False, "externalIp": "", "externalPort": item["port"]})


def get_tunnel_config(server_id):
    _, item = _require_server(server_id)
    config = item.get("tunnel", {})
    return {key: value for key, value in config.items() if key != "secret"} | {"hasSecret": bool(config.get("secret"))}


def save_tunnel_config(server_id, config):
    records, item = _require_server(server_id)
    provider = str(config.get("provider", "bore"))
    if provider not in {"bore", "playit", "custom_bore"}:
        raise ValueError("Tunnel provider không hợp lệ")
    old = item.get("tunnel", {})
    secret = str(config.get("secret", "")) or old.get("secret", "")
    host = str(config.get("relayHost", "bore.pub")).strip()
    port = int(config.get("relayPort", 7835))
    if provider == "custom_bore" and (not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or not 1 <= port <= 65535):
        raise ValueError("Custom bore relay host/port không hợp lệ")
    item["tunnel"] = {"provider": provider, "secret": secret[:512], "relayHost": host, "relayPort": port,
                       "autoStart": bool(config.get("autoStart", False))}
    _write_index(records)
    return get_tunnel_config(server_id)


def enable_port_forward(server_id):
    records, item = _require_server(server_id)
    binary = shutil.which("upnpc")
    if not binary:
        raise DockerError("miniupnpc chưa được cài. Cài gói Debian mới hoặc apt install miniupnpc.")
    route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route.connect(("1.1.1.1", 53))
        local_ip = route.getsockname()[0]
    finally:
        route.close()
    mapping = _host_command([binary, "-e", f"MineServe-{item['id']}", "-a", local_ip, item["port"], item["port"], "TCP"], timeout=30)
    if mapping.returncode:
        raise DockerError((mapping.stderr or mapping.stdout or "Router did not accept UPnP mapping").strip()[-1000:])
    if item.get("bedrockPort"):
        udp = _host_command([binary, "-e", f"MineServe-{item['id']}-Bedrock", "-a", local_ip, item["bedrockPort"], item["bedrockPort"], "UDP"], timeout=30)
        if udp.returncode:
            _host_command([binary, "-d", item["port"], "TCP"])
            raise DockerError((udp.stderr or udp.stdout or "Router rejected Bedrock UDP mapping").strip()[-1000:])
    status = _host_command([binary, "-s"], timeout=20)
    match = re.search(r"ExternalIPAddress\s*=\s*([0-9.]+)", status.stdout + status.stderr)
    item["portForward"] = {"enabled": True, "externalIp": match.group(1) if match else "", "externalPort": item["port"], "localIp": local_ip}
    _write_index(records)
    return item["portForward"]


def disable_port_forward(server_id):
    records, item = _require_server(server_id)
    binary = shutil.which("upnpc")
    if binary:
        _host_command([binary, "-d", item["port"], "TCP"], timeout=30)
        if item.get("bedrockPort"):
            _host_command([binary, "-d", item["bedrockPort"], "UDP"], timeout=30)
    item["portForward"] = {"enabled": False, "externalIp": "", "externalPort": item["port"]}
    _write_index(records)
    return item["portForward"]


def import_world(server_id, archive_path):
    _, item = _require_server(server_id)
    if _inspect(item["container"])[0] == "RUNNING":
        raise ValueError("Dừng server trước khi nhập world")
    root = Path(item["directory"]).resolve()
    with zipfile.ZipFile(archive_path) as source:
        names = source.namelist()
        has_region = any("region/" in name and name.endswith(".mca") for name in names)
        has_bedrock_db = any("db/" in name for name in names)
        if has_bedrock_db and not has_region:
            raise ValueError("Đây là world Bedrock LevelDB. Server Linux cần world Java Anvil (.mca).")
        if not any(name.endswith("level.dat") for name in names) and not has_region:
            raise ValueError("ZIP không giống world Java (thiếu level.dat/region)")
        if len(names) > 200000:
            raise ValueError("World archive có quá nhiều entries")
        if sum(info.file_size for info in source.infolist()) > 20 * 1024 * 1024 * 1024:
            raise ValueError("World archive vượt giới hạn giải nén 20 GB")
        valid = [n for n in names if not n.endswith("/")]
        prefix = ""
        level = next((n for n in names if n.endswith("level.dat")), "")
        if "/" in level:
            prefix = level.rsplit("/", 1)[0] + "/"
        target = root / "world"
        nether_target = root / "world_nether"
        end_target = root / "world_the_end"
        if any((root / name).exists() for name in ("world", "world_nether", "world_the_end")):
            create_backup(server_id, world_only=True)
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(nether_target, ignore_errors=True)
        shutil.rmtree(end_target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        for name in valid:
            normalized = name.replace("\\", "/")
            if normalized.startswith("world_nether/"):
                destination, relative = nether_target, normalized[len("world_nether/"):]
            elif normalized.startswith("world_the_end/"):
                destination, relative = end_target, normalized[len("world_the_end/"):]
            elif prefix and normalized.startswith(prefix):
                destination, relative = target, normalized[len(prefix):]
            elif not prefix:
                destination, relative = target, normalized
            else:
                continue
            output = (destination / relative).resolve()
            if output != destination and destination not in output.parents:
                raise ValueError("ZIP chứa đường dẫn không an toàn")
            output.parent.mkdir(parents=True, exist_ok=True)
            with source.open(name) as src, output.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return {"imported": True}


def reset_dimension(server_id, dimension):
    _, item = _require_server(server_id)
    if _inspect(item["container"])[0] == "RUNNING":
        raise ValueError("Dừng server trước khi reset dimension")
    names = {"nether": ["world_nether", "world/DIM-1"],
             "end": ["world_the_end", "world/DIM1"]}.get(dimension)
    if not names:
        raise ValueError("Dimension không hợp lệ")
    root = Path(item["directory"])
    if any((root / name).exists() for name in ("world", "world_nether", "world_the_end")):
        create_backup(server_id, world_only=True)
    for name in names:
        target = root / name
        if target.is_dir():
            shutil.rmtree(target)
    return {"reset": dimension}


def optimize_world(server_id, threshold_ticks=0, prune_nether=True, prune_end=True):
    _, item = _require_server(server_id)
    if _inspect(item["container"])[0] == "RUNNING":
        raise ValueError("Dừng server trước khi tối ưu region files")
    threshold = int(threshold_ticks)
    if threshold not in {0, 600, 2400, 6000}:
        raise ValueError("Ngưỡng inhabited time không hợp lệ")
    root = Path(item["directory"])
    region_dirs = [root / "world" / "region"]
    if prune_nether:
        region_dirs.extend([root / "world_nether" / "region", root / "world" / "DIM-1" / "region"])
    if prune_end:
        region_dirs.extend([root / "world_the_end" / "region", root / "world" / "DIM1" / "region"])
    files = [path for directory in region_dirs if directory.is_dir() for path in directory.glob("*.mca")]
    if files:
        create_backup(server_id, world_only=True)
    scanned_regions = scanned_chunks = removed_chunks = deleted_regions = 0
    before = sum(path.stat().st_size for path in files)
    for path in files:
        try:
            raw = path.read_bytes()
            if len(raw) < 8192:
                continue
            header = bytearray(raw[:8192])
            keep = []
            occupied = 0
            pruned = 0
            for index in range(1024):
                slot = index * 4
                sector_offset = (raw[slot] << 16) | (raw[slot + 1] << 8) | raw[slot + 2]
                sector_count = raw[slot + 3]
                if not sector_offset or not sector_count:
                    continue
                occupied += 1
                pos = sector_offset * 4096
                if pos + 5 > len(raw):
                    continue
                length = struct.unpack_from(">I", raw, pos)[0]
                if length < 2 or pos + 4 + length > len(raw):
                    continue
                compression = raw[pos + 4]
                record = raw[pos:pos + 4 + length]
                payload = record[5:]
                try:
                    decoded = gzip.decompress(payload) if compression == 1 else zlib.decompress(payload) if compression == 2 else payload if compression == 3 else None
                except (OSError, zlib.error):
                    decoded = None
                if decoded is None:
                    keep.append((index, record, raw[4096 + index * 4:4100 + index * 4]))
                    continue
                marker = b"\x04\x00\rInhabitedTime"
                found = decoded.find(marker)
                inhabited = struct.unpack_from(">q", decoded, found + len(marker))[0] if found >= 0 and found + len(marker) + 8 <= len(decoded) else 0
                if inhabited <= threshold:
                    pruned += 1
                else:
                    keep.append((index, record, raw[4096 + index * 4:4100 + index * 4]))
            scanned_chunks += occupied
            removed_chunks += pruned
            scanned_regions += 1
            if pruned == 0:
                continue
            if not keep:
                path.unlink()
                deleted_regions += 1
                continue
            output = bytearray(8192)
            sector = 2
            for index, record, timestamp in keep:
                sectors = (len(record) + 4095) // 4096
                if sectors > 255 or sector > 0xFFFFFF:
                    raise ValueError(f"Region chunk exceeds Anvil sector limits: {path.name}")
                slot = index * 4
                output[slot:slot + 4] = bytes([(sector >> 16) & 255, (sector >> 8) & 255, sector & 255, sectors])
                output[4096 + slot:4100 + slot] = timestamp
                output.extend(record)
                output.extend(b"\x00" * (sectors * 4096 - len(record)))
                sector += sectors
            temp = path.with_suffix(path.suffix + ".mineserve.tmp")
            temp.write_bytes(output)
            temp.replace(path)
        except (OSError, ValueError):
            continue
    after = sum(path.stat().st_size for directory in region_dirs if directory.is_dir() for path in directory.glob("*.mca"))
    return {"scannedRegions": scanned_regions, "scannedChunks": scanned_chunks, "prunedChunks": removed_chunks,
            "deletedRegions": deleted_regions, "bytesBefore": before, "bytesAfter": after, "bytesFreed": max(0, before-after)}


def get_automation(server_id):
    _, item = _require_server(server_id)
    return item.get("automation", {"idleSleepEnabled": False, "idleSleepTimeoutMinutes": 10,
                                   "autoWakeOnPing": False, "tasks": []})


def set_automation(server_id, config):
    records, item = _require_server(server_id)
    tasks = config.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) > 50:
        raise ValueError("Tối đa 50 scheduled tasks")
    clean_tasks = []
    now = int(time.time())
    for task in tasks:
        task_id = str(task.get("id") or uuid.uuid4().hex[:8])
        kind = str(task.get("type", "world_backup"))
        frequency = str(task.get("frequency", "interval"))
        if kind not in {"world_backup", "full_backup", "restart", "broadcast", "command"}:
            raise ValueError("Task type không hợp lệ")
        if frequency not in {"interval", "daily", "every_days", "weekly", "cron"}:
            raise ValueError("Schedule frequency không hợp lệ")
        interval = int(task.get("intervalMinutes", 360))
        if frequency == "interval" and not 1 <= interval <= 525600:
            raise ValueError("Interval phải từ 1 phút đến 1 năm")
        command = str(task.get("command", ""))[:1024]
        if kind in {"broadcast", "command"} and (not command or "\n" in command or "\r" in command):
            raise ValueError("Task cần command hợp lệ")
        hour = max(0, min(23, int(task.get("hour", 4))))
        minute = max(0, min(59, int(task.get("minute", 0))))
        cron = str(task.get("cron", ""))[:128]
        if frequency == "cron" and not _cron_valid(cron):
            raise ValueError("Cron expression phải có 5 trường hợp lệ")
        day_interval = max(1, min(366, int(task.get("dayInterval", 1))))
        week_interval = max(1, min(52, int(task.get("weekInterval", 1))))
        days = [int(x) for x in task.get("daysOfWeek", []) if str(x).isdigit() and 0 <= int(x) <= 6]
        next_run = int(task.get("nextRunAt") or _next_schedule(frequency, now, interval, hour, minute, cron,
                                                                day_interval, week_interval, days))
        clean_tasks.append({"id": task_id, "name": str(task.get("name", kind))[:80], "enabled": bool(task.get("enabled", True)),
                            "type": kind, "frequency": frequency, "intervalMinutes": interval,
                            "hour": hour, "minute": minute, "dayInterval": day_interval,
                            "weekInterval": week_interval, "daysOfWeek": days,
                            "cron": cron, "command": command,
                            "retention": max(1, min(100, int(task.get("retention", 5)))),
                            "lastRunAt": int(task.get("lastRunAt", 0)), "nextRunAt": next_run})
    item["automation"] = {"idleSleepEnabled": bool(config.get("idleSleepEnabled", False)),
                          "idleSleepTimeoutMinutes": max(1, min(1440, int(config.get("idleSleepTimeoutMinutes", 10)))),
                          "autoWakeOnPing": bool(config.get("autoWakeOnPing", False)), "tasks": clean_tasks}
    _write_index(records)
    return item["automation"]


def delete_automation_task(server_id, task_id):
    config = get_automation(server_id)
    tasks = [task for task in config.get("tasks", []) if task.get("id") != task_id]
    if len(tasks) == len(config.get("tasks", [])):
        raise ValueError("Không tìm thấy task")
    config["tasks"] = tasks
    return set_automation(server_id, config)


def _parse_cron_field(field, value, maximum):
    if field == "*":
        return True
    for part in field.split(","):
        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if step < 1:
                continue
            start, end = (0, maximum) if base == "*" else tuple(map(int, base.split("-", 1))) if "-" in base else (int(base), maximum)
            if start <= value <= end and (value - start) % step == 0:
                return True
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
            if start <= value <= end:
                return True
        elif int(part) == value:
            return True
    return False


def _cron_matches(expression, now):
    fields = expression.split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    values = [now.tm_min, now.tm_hour, now.tm_mday, now.tm_mon, (now.tm_wday + 1) % 7]
    limits = [59, 23, 31, 12, 6]
    try:
        return all(_parse_cron_field(f, v, m) for f, v, m in zip(fields, values, limits))
    except (ValueError, IndexError):
        return False


def _cron_valid(expression):
    fields = expression.split()
    if len(fields) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    try:
        for field, (low, high) in zip(fields, ranges):
            for part in field.split(","):
                base, slash, step = part.partition("/")
                if slash and (not step.isdigit() or int(step) < 1):
                    return False
                if base == "*":
                    continue
                values = base.split("-")
                if len(values) > 2 or any(not x.isdigit() or not low <= int(x) <= high for x in values):
                    return False
                if len(values) == 2 and int(values[0]) > int(values[1]):
                    return False
        return True
    except ValueError:
        return False


def _next_schedule(frequency, now, interval, hour, minute, cron, day_interval=1, week_interval=1, days_of_week=None):
    if frequency == "interval":
        return now + interval * 60
    if frequency == "daily":
        current = datetime.fromtimestamp(now)
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return int(candidate.timestamp())
    if frequency == "every_days":
        current = datetime.fromtimestamp(now)
        candidate = (current + timedelta(days=max(1, day_interval))).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return int(candidate.timestamp())
    if frequency == "weekly":
        selected = set(days_of_week or [0])
        base = datetime.fromtimestamp(now).date()
        base_monday = base - timedelta(days=base.weekday())
        candidate = now - (now % 60) + 60
        for _ in range(week_interval * 7 * 24 * 60):
            current = datetime.fromtimestamp(candidate)
            monday = current.date() - timedelta(days=current.weekday())
            weeks = (monday - base_monday).days // 7
            if weeks % week_interval == 0 and current.weekday() in selected and (current.hour, current.minute) == (hour, minute):
                return candidate
            candidate += 60
        raise ValueError("Không tìm được lần chạy tiếp theo cho lịch tuần")
    candidate = now - (now % 60) + 60
    for _ in range(366 * 24 * 60):
        if _cron_matches(cron, time.localtime(candidate)):
            return candidate
        candidate += 60
    raise ValueError("Không tìm được lần chạy tiếp theo cho cron expression")


def _execute_scheduled_task(server_id, task):
    if task["type"] == "world_backup":
        create_backup(server_id, True)
    elif task["type"] == "full_backup":
        create_backup(server_id, False)
    elif task["type"] == "restart":
        server_action(server_id, "restart")
    elif task["type"] == "broadcast":
        send_command(server_id, "say " + task["command"])
    else:
        send_command(server_id, task["command"])
    if task["type"] in {"world_backup", "full_backup"}:
        entries = list_backups(server_id)
        for entry in entries[task["retention"]:]:
            delete_backup(server_id, entry["name"])


def start_automation_scheduler():
    wake_threads = set()

    def wake_on_ping(item):
        server_id = item["id"]
        while True:
            current = get_server(server_id)
            if not current:
                wake_threads.discard(server_id)
                return
            config = get_automation(server_id)
            if not config.get("autoWakeOnPing"):
                wake_threads.discard(server_id)
                return
            if current["status"] == "RUNNING":
                time.sleep(10)
                continue
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("0.0.0.0", int(current["port"])))
                listener.listen(8)
                listener.settimeout(10)
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                client.close()
                server_action(server_id, "start")
                time.sleep(15)
            except OSError:
                time.sleep(10)
            finally:
                listener.close()

    def loop():
        while True:
            if not shutil.which("docker"):
                time.sleep(30)
                continue
            now = int(time.time())
            now_tm = time.localtime(now)
            for item in _read_index():
                config = item.get("automation", {})
                state, _ = _inspect(item["container"])
                if config.get("autoWakeOnPing") and item["id"] not in wake_threads:
                    wake_threads.add(item["id"])
                    threading.Thread(target=wake_on_ping, args=(item,), name=f"mineserve-wake-{item['id']}", daemon=True).start()
                if config.get("idleSleepEnabled") and state == "RUNNING":
                    try:
                        output = send_command(item["id"], "list")["output"].lower()
                        match = re.search(r"(?:there are\s+(\d+)|online:\s*(\d+)|players online:?\s*(\d+))", output)
                        count = int(next(group for group in match.groups() if group is not None))
                        if count > 0:
                            item.pop("idleSince", None)
                        else:
                            since = int(item.setdefault("idleSince", now))
                            if now - since >= int(config.get("idleSleepTimeoutMinutes", 10)) * 60:
                                server_action(item["id"], "stop")
                        records = _read_index()
                        record = next((x for x in records if x["id"] == item["id"]), None)
                        if record:
                            if item.get("idleSince"):
                                record["idleSince"] = item["idleSince"]
                            else:
                                record.pop("idleSince", None)
                            _write_index(records)
                    except Exception:
                        pass
                for task in config.get("tasks", []):
                    due = int(task.get("nextRunAt", 0)) <= now
                    if task.get("enabled") and due and (task.get("frequency") != "cron" or _cron_matches(task.get("cron", ""), now_tm)):
                        try:
                            _execute_scheduled_task(item["id"], task)
                        except Exception:
                            pass
                        records = _read_index()
                        record = next((x for x in records if x["id"] == item["id"]), None)
                        if record:
                            task_state = next((x for x in record.get("automation", {}).get("tasks", []) if x.get("id") == task.get("id")), None)
                            if task_state:
                                task_state["lastRunAt"] = now
                                task_state["nextRunAt"] = _next_schedule(
                                    task_state["frequency"], now, task_state.get("intervalMinutes", 360),
                                    task_state.get("hour", 4), task_state.get("minute", 0), task_state.get("cron", ""),
                                    task_state.get("dayInterval", 1), task_state.get("weekInterval", 1), task_state.get("daysOfWeek", []))
                                _write_index(records)
            time.sleep(30)
    threading.Thread(target=loop, name="mineserve-automation", daemon=True).start()


def install_java(java_version, progress=None):
    version = int(java_version)
    if version not in JAVA_IMAGES:
        raise ValueError("Java version không được hỗ trợ")
    if not runtime_status()["running"]:
        raise DockerError("Docker Engine chưa sẵn sàng")
    image = f"{IMAGE}:{JAVA_IMAGES[version]}"
    result = _docker(["pull", image], timeout=1800, check=False, capture=False)
    if result.returncode:
        raise DockerError(f"Không tải được Java {version} runtime image")
    return {"javaVersion": version, "image": image, "installed": True}


def reinstall_runtime(progress=None):
    """Reinstall Docker Engine through the narrow helper installed by the Debian package."""
    sudo = shutil.which("sudo")
    helper = Path("/usr/lib/mineserve/runtime-helper")
    if sudo and helper.is_file():
        action = "install" if not shutil.which("docker") else "reinstall"
        result = subprocess.run([sudo, "-n", str(helper), action], capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            return {"runtime": "docker", "reinstalled": True,
                    "message": (result.stdout or "Docker Engine operation completed").strip()}
        raise DockerError("Không có quyền sudo cho thao tác runtime. Cài lại gói MineServe bằng sudo hoặc kiểm tra /etc/sudoers.d/mineserve-runtime.")
    if not shutil.which("docker"):
        raise DockerError("Docker chưa cài; cần cài docker.io với quyền quản trị trên host")
    raise DockerError("Không tìm thấy runtime helper có quyền quản trị; cài lại gói MineServe để bật chức năng này")
