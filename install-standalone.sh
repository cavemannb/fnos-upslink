#!/bin/bash
# 不走 fpk 的安装方式：直接装成 systemd 服务（在飞牛 SSH 里执行）
#   sudo bash install-standalone.sh
# 卸载：
#   sudo systemctl disable --now upslink && sudo rm -rf /opt/upslink /etc/systemd/system/upslink.service
set -e

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash $0"; exit 1; }
command -v python3 >/dev/null || { echo "缺少 python3：apt install -y python3"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/upslink
PORT="${1:-8099}"

mkdir -p "$PREFIX/var"
cp "$SRC/app/server.py" "$PREFIX/"
rm -rf "$PREFIX/public" "$PREFIX/ui"
cp -r "$SRC/app/public" "$PREFIX/"
cp -r "$SRC/app/ui" "$PREFIX/"

cat >/etc/systemd/system/upslink.service <<UNIT
[Unit]
Description=UPSLink - NUT client for fnOS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$PREFIX
Environment=UPSLINK_DATA=$PREFIX/var
Environment=UPSLINK_PORT=$PORT
ExecStart=/usr/bin/python3 $PREFIX/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now upslink
sleep 2

echo
if systemctl is-active --quiet upslink; then
	echo "已启动。管理页面： http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
	echo "数据目录：$PREFIX/var（config.json / events.log / app.log）"
else
	echo "启动失败，看日志： journalctl -u upslink -n 50 --no-pager"
	exit 1
fi
