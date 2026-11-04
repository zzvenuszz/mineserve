#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGE=$(mktemp -d)
trap 'python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$STAGE"' EXIT HUP INT TERM
chmod 0755 "$STAGE"

install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/mineserve" "$STAGE/usr/share/doc/mineserve"
install -m 0644 "$ROOT/debian/control" "$STAGE/DEBIAN/control"
install -m 0755 "$ROOT/debian/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$ROOT/debian/postrm" "$STAGE/DEBIAN/postrm"
install -m 0755 "$ROOT/linux/entrypoint" "$STAGE/usr/bin/mineserve"
install -m 0755 "$ROOT/linux/runtime-helper" "$STAGE/usr/lib/mineserve/runtime-helper"
install -m 0644 "$ROOT/linux/cli.py" "$STAGE/usr/lib/mineserve/cli.py"
install -m 0644 "$ROOT/linux/docker_manager.py" "$STAGE/usr/lib/mineserve/docker_manager.py"
install -m 0644 "$ROOT/linux/webpanel.html" "$STAGE/usr/lib/mineserve/webpanel.html"
install -m 0644 "$ROOT/docker/panel.py" "$STAGE/usr/lib/mineserve/panel.py"
install -m 0644 "$ROOT/README.md" "$STAGE/usr/share/doc/mineserve/README.md"

dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/mineserve.deb"
echo "Created $ROOT/mineserve.deb"
