#!/bin/bash
# 重建关机权限（/etc/sudoers.d/upslink）与能力标记 shutdown_capable。
#
# 谁可以调用它：
#   1) install_callback / cmd/main start —— 本身就以 root 运行；
#   2) 应用进程（降权后的 upslink 用户）—— 通过 sudo 规则里放行的这一条命令。
#
# 安全约定：**不接受任何参数**，路径只来自 root 拥有的 repair.env，
# 因此即使被应用用户调用，也只能做「重写这一份固定内容」这一件事，
# 不构成任意 root 操作。

set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
SUDOERS="/etc/sudoers.d/upslink"

# 由生命周期脚本写入的运行时参数（root 拥有，应用用户不可改）
if [ -f "${SELF_DIR}/repair.env" ]; then
	. "${SELF_DIR}/repair.env"
fi

DATA_DIR="${DATA_DIR:-${TRIM_PKGVAR:-/opt/upslink/var}}"
RUN_USER="${RUN_USER:-${TRIM_USERNAME:-upslink}}"

if [ "$(id -u)" != "0" ]; then
	echo "需要 root 权限" >&2
	exit 1
fi

mkdir -p "$DATA_DIR" 2>/dev/null

cap=0
if id "$RUN_USER" >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
	umask 022
	# 在 sudoers.d 内用临时文件 + mv，避免 sudo 读到半截内容；
	# 文件名不含「.」（sudo 会忽略带点的文件并打警告）。
	tmp="$(mktemp -p /etc/sudoers.d upslink-tmpXXXXXX 2>/dev/null)"
	if [ -n "${tmp:-}" ]; then
		printf '%s ALL=(root) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown, /sbin/poweroff, /usr/sbin/poweroff, %s\n' \
			"$RUN_USER" "${SELF_DIR}/repair_shutdown.sh" >"$tmp"
		chmod 0440 "$tmp"
		if command -v visudo >/dev/null 2>&1 && ! visudo -cf "$tmp" >/dev/null 2>&1; then
			rm -f "$tmp"
			tmp=""
		fi
		if [ -n "${tmp:-}" ]; then
			if mv -f "$tmp" "$SUDOERS" 2>/dev/null; then
				chmod 0440 "$SUDOERS" 2>/dev/null
				cap=1
			else
				rm -f "$tmp"
			fi
		fi
	fi
fi

echo "$cap" >"${DATA_DIR}/shutdown_capable" 2>/dev/null
chmod 0644 "${DATA_DIR}/shutdown_capable" 2>/dev/null
if id "$RUN_USER" >/dev/null 2>&1; then
	chown "${RUN_USER}:${RUN_USER}" "${DATA_DIR}/shutdown_capable" 2>/dev/null
fi

if [ "$cap" = "1" ]; then
	echo "关机权限已写入 ${SUDOERS}"
	exit 0
fi

echo "未能写入 ${SUDOERS}（用户 ${RUN_USER} 或 sudo/visudo 不可用）" >&2
exit 1
