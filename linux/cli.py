#!/usr/bin/python3
"""Terminal control menu for the MineServe Linux edition."""

import getpass
import ipaddress
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tarfile
import urllib.error
import urllib.request
from pathlib import Path


APP_NAME = "mineserve"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
BIN_DIR = Path.home() / ".local/bin"
os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
DEFAULTS = {"username": "admin", "password": "", "port": 8080,
            "exposure": "local", "ngrok_token": "", "ram_mb": 2048,
            "max_ram_mb": 8192, "mc_port": 25565}
EXPOSURE_LABELS = {"local": "Chỉ máy này", "lan": "Mạng LAN", "ngrok": "ngrok", "playit": "playit.gg", "bore": "bore.pub"}


def read_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    try:
        cfg = {**DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
    except (OSError, json.JSONDecodeError):
        cfg = DEFAULTS.copy()
    if CONFIG_FILE.exists():
        CONFIG_FILE.chmod(0o600)
    if not cfg["password"]:
        print("Thiết lập thông tin đăng nhập web panel lần đầu.")
        cfg["username"] = input("Tên đăng nhập [admin]: ").strip() or "admin"
        while True:
            password = getpass.getpass("Mật khẩu (tối thiểu 8 ký tự): ")
            if len(password) >= 8:
                break
            print("Mật khẩu cần có ít nhất 8 ký tự.")
        confirmation = getpass.getpass("Nhập lại mật khẩu: ")
        if password != confirmation:
            print("Mật khẩu không khớp.")
            return read_config()
        cfg["password"] = password
        save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(CONFIG_FILE)


def panel_script():
    installed = Path("/usr/lib/mineserve/panel.py")
    if installed.exists():
        return installed
    return Path(__file__).resolve().parents[1] / "docker/panel.py"


def linux_arch():
    machine = platform.machine().lower()
    aliases = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
               "arm64": "arm64", "armv7l": "armv7", "armv7": "armv7",
               "i386": "i686", "i686": "i686"}
    if machine not in aliases:
        raise RuntimeError(f"Kiến trúc Linux chưa được hỗ trợ tự cài tunnel: {machine}")
    return aliases[machine]


def fetch_bytes(url, max_bytes=64 * 1024 * 1024):
    request = urllib.request.Request(url, headers={
        "User-Agent": "MineServe/1.1 (https://github.com/devwithzachary/MineServe)",
        "Accept": "application/vnd.github+json, application/octet-stream, */*",
    })
    with urllib.request.urlopen(request, timeout=45) as response:
        chunks = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError("Tệp tunnel tải về vượt kích thước cho phép")
            chunks.append(chunk)
    return b"".join(chunks)


def github_release_asset(repository, asset_name):
    metadata = json.loads(fetch_bytes(f"https://api.github.com/repos/{repository}/releases/latest", 2 * 1024 * 1024))
    asset = next((item for item in metadata.get("assets", []) if item.get("name") == asset_name), None)
    if not asset:
        raise RuntimeError(f"Không tìm thấy asset {asset_name} trong release của {repository}")
    data = fetch_bytes(asset["browser_download_url"])
    digest = asset.get("digest", "")
    if digest.startswith("sha256:"):
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != digest.split(":", 1)[1].lower():
            raise RuntimeError("SHA-256 của tunnel binary không khớp release metadata")
    return data


def extract_tar_binary(archive_data, binary_name):
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if Path(member.name).name == binary_name and member.isfile():
                stream = archive.extractfile(member)
                if stream:
                    return stream.read()
    raise RuntimeError(f"Không tìm thấy binary {binary_name} trong archive")


def install_tunnel(mode):
    if mode not in {"ngrok", "playit", "bore"}:
        return
    if shutil.which(mode):
        return
    arch = linux_arch()
    print(f"Đang tự cài {EXPOSURE_LABELS[mode]} vào {BIN_DIR} …")
    if mode == "ngrok":
        ngrok_arch = {"amd64": "amd64", "arm64": "arm64", "armv7": "arm", "i686": "386"}.get(arch)
        if not ngrok_arch:
            raise RuntimeError(f"ngrok chưa có binary phù hợp cho {arch}")
        url = f"https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-linux-{ngrok_arch}.tgz"
        binary = extract_tar_binary(fetch_bytes(url), "ngrok")
    elif mode == "playit":
        asset_arch = {"amd64": "amd64", "arm64": "aarch64", "armv7": "armv7", "i686": "i686"}.get(arch)
        if not asset_arch:
            raise RuntimeError(f"playit chưa có binary phù hợp cho {arch}")
        binary = github_release_asset("playit-cloud/playit-agent", f"playit-linux-{asset_arch}")
    else:
        target_arch = {"amd64": "x86_64", "arm64": "aarch64", "armv7": "armv7", "i686": "i686"}.get(arch)
        if not target_arch:
            raise RuntimeError(f"bore chưa có binary phù hợp cho {arch}")
        metadata = json.loads(fetch_bytes("https://api.github.com/repos/ekzhang/bore/releases/latest", 2 * 1024 * 1024))
        release_asset = next((item for item in metadata.get("assets", [])
                              if item.get("name", "").startswith("bore-v")
                              and item.get("name", "").endswith(f"-{target_arch}-unknown-linux-musl.tar.gz")), None)
        if not release_asset:
            raise RuntimeError(f"Không tìm thấy bore binary phù hợp cho {arch}")
        archive = fetch_bytes(release_asset["browser_download_url"])
        digest = release_asset.get("digest", "")
        if digest.startswith("sha256:") and hashlib.sha256(archive).hexdigest().lower() != digest.split(":", 1)[1].lower():
            raise RuntimeError("SHA-256 của bore binary không khớp release metadata")
        binary = extract_tar_binary(archive, "bore")

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / mode
    temporary = BIN_DIR / f".{mode}.download"
    temporary.write_bytes(binary)
    temporary.chmod(0o700)
    temporary.replace(target)
    result = subprocess.run([str(target), "--help"], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Binary {mode} tải về không chạy được: {(result.stderr or result.stdout).strip()}")
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")
    print(f"Đã cài {mode} vào {target}")


def prepare_exposure(cfg):
    if cfg.get("exposure") != "ngrok" or cfg.get("ngrok_token"):
        return True
    print("Ngrok yêu cầu tài khoản và authtoken; tunnel random không xác thực không khả dụng.")
    print("1) Chuyển sang bore.pub (cấp cổng random, không cần token)")
    print("2) Nhập ngrok authtoken")
    print("0) Hủy, giữ nguyên tiến trình hiện tại")
    choice = input("Chọn [1]: ").strip() or "1"
    if choice == "1":
        install_tunnel("bore")
        cfg["exposure"] = "bore"
        save_config(cfg)
        print("Đã chọn bore.pub.")
        return True
    if choice == "2":
        token = getpass.getpass("ngrok authtoken: ").strip()
        if not token:
            print("Chưa nhập token; giữ nguyên cấu hình.")
            return False
        cfg["ngrok_token"] = token
        save_config(cfg)
        return True
    return False


def running_pid():
    pidfile = DATA_DIR / "panel.pid"
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        if b"panel.py" in cmdline:
            return pid
    except (OSError, ValueError):
        pass
    return None


def start_panel(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if running_pid():
        return "MineServe panel đang chạy; không tạo tiến trình khác."
    if cfg["exposure"] in {"ngrok", "playit", "bore"}:
        try:
            install_tunnel(cfg["exposure"])
        except Exception as exc:
            return f"Không cài được tunnel {cfg['exposure']}: {exc}"
    script = panel_script()
    if not script.is_file():
        return f"Không tìm thấy web panel: {script}"
    env = os.environ.copy()
    env.update({
        "MINESERVE_DATA": str(DATA_DIR), "MINESERVE_USER": cfg["username"],
        "MINESERVE_PASSWORD": cfg["password"], "MINESERVE_EXPOSURE": cfg["exposure"],
        "PANEL_BIND": "0.0.0.0" if cfg["exposure"] == "lan" else "127.0.0.1",
        "WEB_PORT": str(cfg["port"]), "MAX_RAM_MB": str(cfg["max_ram_mb"]),
        "MC_PORT": str(cfg["mc_port"]), "NGROK_AUTHTOKEN": cfg.get("ngrok_token", ""),
    })
    log = (DATA_DIR / "panel.log").open("a", buffering=1)
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True, close_fds=True)
    log.close()
    for _ in range(40):
        time.sleep(0.15)
        if running_pid():
            if cfg["exposure"] in {"ngrok", "playit", "bore"}:
                for _ in range(20):
                    time.sleep(0.5)
                    addresses = public_addresses()
                    if addresses:
                        return "Panel đã khởi chạy."
                return "Panel đã khởi chạy."
            return "Panel đã khởi chạy."
        if proc.poll() is not None:
            break
    return "Không khởi chạy được panel. Chọn mục Xem log để biết chi tiết."


def stop_panel():
    pid = running_pid()
    if not pid:
        return "MineServe panel hiện không chạy."
    os.kill(pid, signal.SIGTERM)
    for _ in range(80):
        time.sleep(0.15)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "Đã dừng MineServe panel."
    return "Đã gửi yêu cầu dừng; tiến trình đang hoàn tất shutdown."


def save_and_apply(cfg, old, force_restart=False):
    save_config(cfg)
    message = "Đã lưu cấu hình."
    if running_pid() and (cfg != old or force_restart):
        if input("Khởi động lại panel để áp dụng? [Y/n]: ").strip().lower() != "n":
            stop_message = stop_panel()
            start_message = start_panel(cfg)
            message = f"{stop_message} {start_message}"
    return message


def configure_credentials(cfg):
    old = json.loads(json.dumps(cfg))
    username = input(f"Tên đăng nhập [{cfg['username']}]: ").strip()
    if username:
        cfg["username"] = username
    if input("Đổi mật khẩu? [y/N]: ").strip().lower() == "y":
        password = getpass.getpass("Mật khẩu mới (tối thiểu 8 ký tự): ")
        if len(password) < 8:
            raise ValueError("Mật khẩu cần có ít nhất 8 ký tự")
        if password != getpass.getpass("Nhập lại mật khẩu mới: "):
            raise ValueError("Mật khẩu không khớp")
        cfg["password"] = password
    save_and_apply(cfg, old)


def configure_port(cfg):
    old = json.loads(json.dumps(cfg))
    value = input(f"Cổng web panel hiện là {cfg['port']}. Cổng mới: ").strip()
    if not value:
        return
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("Cổng phải nằm trong khoảng 1–65535")
    cfg["port"] = port
    save_and_apply(cfg, old)


def configure_exposure(cfg):
    old = json.loads(json.dumps(cfg))
    print("\nKiểu truy cập:")
    for key, label in EXPOSURE_LABELS.items():
        print(f"  {key}: {label}")
    mode = input(f"Chọn kiểu [{cfg['exposure']}]: ").strip().lower() or cfg["exposure"]
    if mode not in EXPOSURE_LABELS:
        raise ValueError("Kiểu truy cập không hợp lệ")
    cfg["exposure"] = mode
    if not prepare_exposure(cfg):
        return
    mode = cfg["exposure"]
    installed_before = shutil.which(mode) if mode in {"ngrok", "playit", "bore"} else True
    if mode in {"ngrok", "playit", "bore"}:
        install_tunnel(mode)
    if mode == "playit" and shutil.which("playit"):
        if input("Chạy playit setup để claim agent ngay? [y/N]: ").strip().lower() == "y":
            result = subprocess.run([shutil.which("playit"), "setup"])
            if result.returncode:
                raise ValueError("playit setup chưa hoàn tất")
    save_and_apply(cfg, old, force_restart=not installed_before)


def configure_tunnel_credentials(cfg):
    old = json.loads(json.dumps(cfg))
    print("Token là tùy chọn. Để trống để dùng chế độ random/anonymous nếu agent hỗ trợ.")
    token = getpass.getpass("ngrok authtoken mới (Enter giữ nguyên, ! xóa): ")
    if token == "!":
        cfg["ngrok_token"] = ""
    elif token:
        cfg["ngrok_token"] = token
    if cfg.get("exposure") == "ngrok" and not cfg.get("ngrok_token"):
        if not prepare_exposure(cfg):
            return
    save_and_apply(cfg, old)


def clear_screen():
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def configure_menu(cfg):
    notice = ""
    while True:
        clear_screen()
        print("=== Cấu hình MineServe ===")
        if notice:
            print(f"\n{notice}")
        print("\n1) Tài khoản và mật khẩu")
        print("2) Cổng web panel")
        print("3) Cách truy cập / tunnel")
        print("4) Token ngrok (tùy chọn)")
        print("0) Quay về menu chính")
        choice = input("Chọn mục cấu hình: ").strip()
        clear_screen()
        try:
            if choice == "1":
                configure_credentials(cfg)
                notice = "Đã cập nhật thông tin tài khoản."
            elif choice == "2":
                configure_port(cfg)
                notice = "Đã cập nhật cổng web panel."
            elif choice == "3":
                configure_exposure(cfg)
                notice = "Đã cập nhật cách truy cập."
            elif choice == "4":
                configure_tunnel_credentials(cfg)
                notice = "Đã cập nhật token ngrok."
            elif choice == "0":
                return cfg
            else:
                notice = "Lựa chọn không hợp lệ."
        except (ValueError, OSError, RuntimeError) as exc:
            notice = f"Lỗi: {exc}"


def public_addresses():
    if not running_pid():
        return []
    try:
        lines = (DATA_DIR / "panel.log").read_text(errors="replace").splitlines()
    except OSError:
        return []
    session_start = 0
    for index, line in enumerate(lines):
        if "Web panel listening on" in line:
            session_start = index
    domain_port = re.compile(r"(?<![\w.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})(?::(\d{1,5}))?")
    ipv4_port = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3}):(\d{1,5})(?!\d)")
    for line in reversed(lines[session_start:]):
        if not re.search(r"\[(?:ngrok|playit|bore)\]", line, re.IGNORECASE):
            continue
        if re.search(r"playit\.gg/(?:account|claim)", line, re.IGNORECASE):
            continue
        match = domain_port.search(line)
        if match:
            host, port = match.groups()
            if not port:
                port = "443" if "https://" in line.lower() else "80"
            if 1 <= int(port) <= 65535:
                return [f"{host}:{port}"]
        match = ipv4_port.search(line)
        if match:
            host, port = match.groups()
            try:
                ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                continue
            if 1 <= int(port) <= 65535:
                return [f"{host}:{port}"]
    return []


_PUBLIC_IP_CACHE = {"checkedAt": 0, "value": ""}


def public_ip():
    now = time.time()
    if now - _PUBLIC_IP_CACHE["checkedAt"] < 300:
        return _PUBLIC_IP_CACHE["value"]
    value = ""
    try:
        request = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "MineServe/2.1"})
        with urllib.request.urlopen(request, timeout=2) as response:
            candidate = response.read(64).decode("ascii", errors="ignore").strip()
        value = str(ipaddress.ip_address(candidate))
    except (OSError, ValueError, urllib.error.URLError):
        pass
    _PUBLIC_IP_CACHE.update(checkedAt=now, value=value)
    return value


def print_status(cfg):
    pid = running_pid()
    print(f"Panel: {'đang chạy (PID '+str(pid)+')' if pid else 'đang dừng'}")
    print(f"URL local: http://127.0.0.1:{cfg['port']}")
    if pid and cfg["exposure"] not in {"ngrok", "playit", "bore"}:
        ip = public_ip()
        if ip:
            print(f"IP public của host: {ip}")
    if cfg["exposure"] == "lan":
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"URL LAN: http://{ip}:{cfg['port']}")
        except OSError:
            pass
    print(f"Chế độ public: {EXPOSURE_LABELS[cfg['exposure']]}")
    if cfg["exposure"] in {"ngrok", "playit", "bore"}:
        addresses = public_addresses()
        if addresses:
            print(f"Panel IP: {addresses[0]}")
        elif pid:
            print("Panel IP: đang chờ tunnel cấp địa chỉ.")
        else:
            print("Panel IP: chưa có vì panel đang dừng.")


def menu():
    cfg = read_config()
    notice = ""
    while True:
        clear_screen()
        print("=== MineServe Linux ===")
        if notice:
            print(f"\n{notice}")
        print_status(cfg)
        print("\n1) Khởi chạy / khởi động lại panel")
        print("2) Cấu hình")
        print("3) Dừng web panel (container server vẫn chạy)")
        print("4) Xem log")
        print("5) Ẩn menu, để panel chạy nền")
        print("0) Thoát menu")
        choice = input("Chọn: ").strip()
        clear_screen()
        try:
            if choice == "1":
                if not prepare_exposure(cfg):
                    notice = "Chưa thể khởi chạy panel; kiểm tra cấu hình truy cập."
                    continue
                stop_message = stop_panel() if running_pid() else ""
                start_message = start_panel(cfg)
                notice = " ".join(part for part in (stop_message, start_message) if part)
            elif choice == "2":
                cfg = configure_menu(cfg)
                notice = "Đã quay về menu chính."
            elif choice == "3":
                notice = stop_panel()
            elif choice == "4":
                clear_screen()
                print("=== Nhật ký MineServe ===\n")
                try:
                    print((DATA_DIR / "panel.log").read_text(errors="replace")[-12000:])
                except OSError:
                    print("Chưa có nhật ký.")
                input("\nNhấn Enter để quay về menu chính…")
                notice = ""
            elif choice in {"5", "0", "q", "Q"}:
                clear_screen()
                print("Đã đóng menu terminal. Panel tiếp tục chạy nền nếu đang bật.")
                return
            else:
                notice = "Lựa chọn không hợp lệ."
        except (ValueError, OSError, RuntimeError) as exc:
            notice = f"Lỗi: {exc}"


if __name__ == "__main__":
    try:
        menu()
    except (EOFError, KeyboardInterrupt):
        print("\nĐã ẩn menu; panel nền vẫn tiếp tục chạy.")
