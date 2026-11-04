# MineServe for Linux

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
[![Discord Community](https://img.shields.io/badge/Discord-Join%20Community-5865F2.svg?logo=discord&logoColor=white)](https://discord.gg/csGrrg5MGF)
[![Original MineServe project](https://img.shields.io/badge/Original%20project-MineServe-blue)](https://github.com/devwithzachary/MineServe)

**MineServe for Linux** is a Docker-based Minecraft server manager for Debian and Ubuntu. It runs each Minecraft server in a separate container and provides a browser-based admin panel for setup and daily management. Use the `mineserve` terminal command to configure and control the panel.

**MineServe for Linux** là công cụ quản lý máy chủ Minecraft bằng Docker dành cho Debian và Ubuntu. Mỗi máy chủ Minecraft chạy trong một container riêng; bảng điều khiển web hỗ trợ cấu hình và quản lý hằng ngày. Dùng lệnh terminal `mineserve` để cấu hình và điều khiển panel.

> This repository preserves the original MineServe project attribution. The Linux edition described here is separate from the original Android application.
>
> Repository này giữ nguyên ghi nhận nguồn gốc MineServe. Bản Linux được mô tả ở đây hoạt động riêng với ứng dụng Android gốc.

## Features / Tính năng

* Manage multiple servers: Paper, Purpur, Paper + Geyser cross-play, Vanilla, Fabric, Forge, NeoForge, and Folia.
* Create servers with a name, MOTD, Minecraft version, initial and maximum RAM, and Java/Bedrock ports; EULA acceptance is required.
* Manage server lifecycle, console, logs, command macros, files, plugins/mods, players, backups, automation, and worlds.
* Browse and edit text files, upload files, search and install compatible projects from Modrinth, and import modpacks where supported.
* Import/export worlds, reset dimensions, optimize world storage, and install/use Squaremap on supported servers.
* View CPU, memory, disk usage, TPS/MSPT, JVM and game-loop health, process diagnostics, and server storage details.
* Check Docker Engine status, reinstall the MineServe runtime, and install Java 8, 17, 21, or 25 server images in App Settings.
* Connect the panel locally, over LAN, or through ngrok, playit.gg, or bore.pub. Missing tunnel agents are downloaded on demand.

* Quản lý nhiều máy chủ: Paper, Purpur, Paper + Geyser cross-play, Vanilla, Fabric, Forge, NeoForge và Folia.
* Tạo máy chủ với tên, MOTD, phiên bản Minecraft, RAM tối thiểu/tối đa và cổng Java/Bedrock; cần chấp nhận EULA.
* Quản lý trạng thái máy chủ, console, log, lệnh nhanh, tệp, plugin/mod, người chơi, bản sao lưu, lịch tự động và thế giới.
* Duyệt và sửa tệp văn bản, tải tệp lên, tìm và cài dự án tương thích từ Modrinth, nhập modpack nếu nền tảng hỗ trợ.
* Nhập/xuất thế giới, đặt lại dimension, tối ưu dung lượng thế giới và cài/dùng Squaremap trên máy chủ được hỗ trợ.
* Theo dõi CPU, RAM, dung lượng đĩa, TPS/MSPT, tình trạng JVM và game loop, chẩn đoán tiến trình và dung lượng máy chủ.
* Kiểm tra Docker Engine, cài đặt lại runtime MineServe và cài image máy chủ Java 8, 17, 21 hoặc 25 trong App Settings.
* Truy cập panel nội bộ, qua LAN hoặc qua ngrok, playit.gg, bore.pub. Tunnel agent còn thiếu sẽ được tải tự động khi cần.

## Requirements / Yêu cầu

* Debian or Ubuntu Linux and a CPU architecture supported by Docker and the selected Minecraft images.
* Python 3.10 or newer, Docker Engine, sudo, and miniupnpc. These are declared as Debian package dependencies.
* Internet access to download MineServe tunnel agents, Minecraft container images, and server files.
* Sufficient disk space and RAM for Docker images, worlds, backups, and running Minecraft servers.

* Debian hoặc Ubuntu Linux, kiến trúc CPU được Docker và image Minecraft đã chọn hỗ trợ.
* Python 3.10 trở lên, Docker Engine, sudo và miniupnpc. Đây là các dependency được khai báo trong gói Debian.
* Kết nối Internet để tải tunnel agent, image Minecraft và tệp máy chủ.
* Đủ dung lượng đĩa và RAM cho Docker image, thế giới, bản sao lưu và máy chủ Minecraft đang chạy.

## Install / Cài đặt

Build and install the Debian package from a checkout:

```bash
git clone https://github.com/devwithzachary/MineServe.git
cd MineServe
./build-deb.sh
sudo apt install ./mineserve.deb
```

The package adds the installing account to the `docker` group and installs a restricted sudo rule for the App Settings runtime action. Log out and back in (or start a new login session) before using Docker through MineServe. Docker group membership grants broad control over the host.

Tạo và cài gói Debian từ mã nguồn:

```bash
git clone https://github.com/devwithzachary/MineServe.git
cd MineServe
./build-deb.sh
sudo apt install ./mineserve.deb
```

Gói sẽ thêm tài khoản cài đặt vào nhóm `docker` và cài sudo rule giới hạn cho thao tác runtime trong App Settings. Hãy đăng xuất rồi đăng nhập lại (hoặc mở login session mới) trước khi dùng Docker qua MineServe. Thành viên nhóm Docker có quyền kiểm soát rộng đối với host.

## First launch and panel access / Khởi chạy và truy cập panel

Run `mineserve`. On first launch, set a panel username and password; the password must be at least 8 characters. The terminal menu lets you start/restart the panel, configure credentials, panel port and exposure mode, view logs, stop the panel, or hide the menu while leaving the panel running. Running `mineserve` again opens the menu for the existing panel process rather than starting another instance.

Open `http://127.0.0.1:8080` on the host by default and sign in. Change the port in **Cấu hình → Cổng web panel** if needed. The panel is a terminal-managed background process, not a desktop tray application, and does not start automatically at boot.

Chạy `mineserve`. Ở lần khởi chạy đầu, hãy đặt tên đăng nhập và mật khẩu panel; mật khẩu cần ít nhất 8 ký tự. Menu terminal cho phép khởi chạy/khởi động lại panel, cấu hình tài khoản, cổng panel và chế độ truy cập, xem log, dừng panel hoặc ẩn menu trong khi panel tiếp tục chạy. Chạy lại `mineserve` sẽ mở menu cho tiến trình panel hiện tại thay vì tạo instance khác.

Mặc định, mở `http://127.0.0.1:8080` trên host và đăng nhập. Đổi cổng tại **Cấu hình → Cổng web panel** nếu cần. Panel chạy nền và được quản lý qua terminal, không phải ứng dụng khay hệ thống; panel không tự khởi chạy khi boot.

### Exposure modes / Các chế độ truy cập

| Mode | English | Tiếng Việt |
| --- | --- | --- |
| Local | Binds to `127.0.0.1`; accessible only from the host. | Chỉ bind vào `127.0.0.1`, chỉ truy cập được từ host. |
| LAN | Binds to host interfaces; use the LAN address shown by MineServe. | Bind vào các interface của host; dùng địa chỉ LAN MineServe hiển thị. |
| ngrok | Requires an ngrok account and a valid authtoken. Configure it in the terminal menu. | Cần tài khoản ngrok và authtoken hợp lệ. Cấu hình token trong menu terminal. |
| playit.gg | Claim the agent profile once with `playit setup`, then select playit in MineServe. | Claim agent một lần bằng `playit setup`, sau đó chọn playit trong MineServe. |
| bore.pub | Provides a random public address without an account token. | Cấp địa chỉ public ngẫu nhiên, không cần token tài khoản. |

Tunnel agents are installed into `~/.local/bin` on demand. The terminal menu shows the public panel address when the tunnel connects. Public access exposes the admin panel; use a trusted tunnel/network and a strong password.

Tunnel agent được cài vào `~/.local/bin` khi cần. Menu terminal hiển thị địa chỉ panel public sau khi tunnel kết nối. Truy cập public sẽ mở panel quản trị ra Internet; hãy dùng tunnel/mạng đáng tin cậy và mật khẩu mạnh.

## Create and manage servers / Tạo và quản lý máy chủ

In the web panel, choose **Tạo server**, enter a name and MOTD, select a platform and available Minecraft version, set initial/maximum RAM and ports, accept the Minecraft EULA, and create the server. Paper + Geyser also configures a Bedrock UDP port. Each server uses an `itzg/minecraft-server` Docker image with persistent files.

The panel provides server overview and settings, Console, Performance, Files, Plugins/Mods, Players, Backups, World, Automation, and Squaremap when supported. App Settings shows Docker Engine status, offers a runtime reinstall action, and installs Java server images with one button. Minecraft images are pulled as needed.

Trong web panel, chọn **Tạo server**, nhập tên và MOTD, chọn platform cùng phiên bản Minecraft, đặt RAM tối thiểu/tối đa và cổng, chấp nhận Minecraft EULA rồi tạo máy chủ. Paper + Geyser cũng cấu hình cổng UDP cho Bedrock. Mỗi máy chủ dùng Docker image `itzg/minecraft-server` và lưu dữ liệu bền vững.

Panel có các mục tổng quan và cấu hình máy chủ, Console, Performance, Files, Plugins/Mods, Players, Backups, World, Automation và Squaremap nếu được hỗ trợ. App Settings hiển thị trạng thái Docker Engine, có chức năng cài đặt lại runtime và cài image Java chỉ bằng một nút. Minecraft image sẽ được tải khi cần.

## Data and logs / Dữ liệu và log

Default paths (respecting `XDG_CONFIG_HOME` and `XDG_DATA_HOME` when set):

* Settings and credentials: `~/.config/mineserve/`
* Panel logs, server files, and backups: `~/.local/share/mineserve/`
* Per-server files: `~/.local/share/mineserve/servers/`

The terminal menu's **Xem log** displays panel and tunnel logs. Minecraft console output and server logs are available from the server's panel pages.

Đường dẫn mặc định (tôn trọng `XDG_CONFIG_HOME` và `XDG_DATA_HOME` nếu được đặt):

* Cấu hình và thông tin đăng nhập: `~/.config/mineserve/`
* Log panel, tệp máy chủ và bản sao lưu: `~/.local/share/mineserve/`
* Tệp từng máy chủ: `~/.local/share/mineserve/servers/`

Mục **Xem log** trong menu terminal hiển thị log panel và tunnel. Console Minecraft và log máy chủ nằm trong các trang quản lý máy chủ trên panel.

## Uninstall / Gỡ cài đặt

Remove the package with `sudo apt remove mineserve`. This leaves the MineServe settings and server data in your home directory. Remove `~/.config/mineserve` and `~/.local/share/mineserve` separately only if you also want to delete configuration, worlds, logs, and backups.

Gỡ gói bằng `sudo apt remove mineserve`. Thao tác này giữ lại cấu hình và dữ liệu MineServe trong thư mục home. Chỉ xóa riêng `~/.config/mineserve` và `~/.local/share/mineserve` nếu bạn cũng muốn xóa cấu hình, thế giới, log và bản sao lưu.

## Original authorship, credits, and license / Nguồn gốc, ghi nhận và giấy phép

This Linux edition continues the **MineServe** project. Preserve the original project source and author attribution: [devwithzachary/MineServe](https://github.com/devwithzachary/MineServe), the [Google Play application](https://play.google.com/store/apps/details?id=com.devwithzachary.mineserve), and the [project community](https://discord.gg/csGrrg5MGF). Please retain these credits when redistributing or modifying the project.

Bản Linux này tiếp nối dự án **MineServe**. Giữ nguyên nguồn mã và ghi nhận tác giả ban đầu: [devwithzachary/MineServe](https://github.com/devwithzachary/MineServe), [ứng dụng trên Google Play](https://play.google.com/store/apps/details?id=com.devwithzachary.mineserve) và [cộng đồng dự án](https://discord.gg/csGrrg5MGF). Vui lòng giữ các ghi nhận này khi phân phối hoặc chỉnh sửa dự án.

MineServe is licensed under **GNU GPL v3.0**; see [LICENSE](LICENSE). Minecraft and the listed server platforms remain the property of their respective owners. Relevant open-source components include [PaperMC/Folia](https://papermc.io/), [Purpur](https://purpurmc.org/), [Fabric](https://fabricmc.net/), [NeoForged](https://neoforged.net/), [Forge](https://forums.minecraftforge.net/), [Geyser/Floodgate](https://geysermc.org/), [Squaremap](https://github.com/jpenilla/squaremap), [Modrinth](https://modrinth.com/), [bore](https://github.com/ekzhang/bore), and [playit.gg](https://playit.gg/); each retains its own license.

MineServe được cấp phép theo **GNU GPL v3.0**; xem [LICENSE](LICENSE). Minecraft và các nền tảng máy chủ nêu trên thuộc quyền sở hữu của chủ sở hữu tương ứng. Các thành phần mã nguồn mở liên quan gồm [PaperMC/Folia](https://papermc.io/), [Purpur](https://purpurmc.org/), [Fabric](https://fabricmc.net/), [NeoForged](https://neoforged.net/), [Forge](https://forums.minecraftforge.net/), [Geyser/Floodgate](https://geysermc.org/), [Squaremap](https://github.com/jpenilla/squaremap), [Modrinth](https://modrinth.com/), [bore](https://github.com/ekzhang/bore) và [playit.gg](https://playit.gg/); mỗi dự án giữ giấy phép riêng.

Special thanks to the original Patreon supporters: **Old PC Gunk (and stuff)** and **насэр Хорр**.

Xin cảm ơn những người ủng hộ Patreon của dự án gốc: **Old PC Gunk (and stuff)** và **насэр Хорр**.

## Contributing / Đóng góp

Bug reports and contributions are welcome through the [project issue tracker](https://github.com/devwithzachary/MineServe/issues). Review [AI.md](AI.md) for the project's AI usage policy.

Hoan nghênh báo lỗi và đóng góp qua [trang issue của dự án](https://github.com/devwithzachary/MineServe/issues). Xem [AI.md](AI.md) để đọc chính sách sử dụng AI của dự án.
