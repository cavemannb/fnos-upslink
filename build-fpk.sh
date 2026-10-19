#!/bin/bash
# 打 .fpk 包（应用中心「手动安装」上传的就是它）
# 真正的打包逻辑在 tools/build_fpk.py，这里只做环境转接。
set -e
cd "$(dirname "$0")"

if [ ! -f ICON_256.PNG ] || [ ! -f ICON.PNG ]; then
	echo "生成图标..."
	"${PYTHON:-python3}" tools/gen_icons.py
fi

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
	for c in python3 python py; do
		command -v "$c" >/dev/null 2>&1 && PY="$c" && break
	done
fi
[ -n "$PY" ] || { echo "缺少 python3，无法打包"; exit 1; }

"$PY" tools/build_fpk.py
