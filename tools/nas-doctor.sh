#!/bin/bash
# 飞牛侧只读体检（不改任何东西）：把 fpk 装不上时该看的东西一次性打出来
#   sudo bash nas-doctor.sh
# 全程只读：只看状态、只读日志，不删除、不修改、不重启任何服务。

line() { printf '\n===== %s =====\n' "$1"; }

line "1. 系统版本与架构"
for f in /etc/fnos-release /etc/trim-release /etc/os-release; do
	[ -f "$f" ] && { echo "--- $f"; cat "$f"; }
done
echo "内核: $(uname -r)  架构: $(uname -m)"
command -v appcenter-cli >/dev/null 2>&1 && { echo "--- appcenter-cli --help"; appcenter-cli --help 2>&1 | head -25; }

line "2. 磁盘空间"
df -h / /vol* 2>/dev/null | grep -v '^Filesystem' | sort -u

line "3. upslink 残留（安装失败通常会留下空壳）"
ls -ld /var/apps/upslink 2>/dev/null
ls -ld /vol*/@appcenter/upslink /vol*/@appdata/upslink /vol*/@appconf/upslink 2>/dev/null
ls -l /var/apps/upslink/cmd 2>/dev/null | head
id upslink 2>/dev/null || echo "（还没有 upslink 用户）"
ls -l /etc/sudoers.d/ 2>/dev/null | grep -i upslink || echo "（没有 upslink 的 sudo 规则）"

line "4. 应用中心服务状态"
systemctl is-active trim_app_center 2>/dev/null
systemctl show -p ActiveEnterTimestamp trim_app_center 2>/dev/null

line "5. 最近 30 分钟内与 upslink 有关的报错"
journalctl -u trim_app_center --since "30 min ago" --no-pager 2>/dev/null | grep -i -E 'upslink|1023|install error' | tail -20 || echo "（journalctl 无输出，改看文件日志）"
grep -i -E 'upslink|1023|install error' /var/log/trim_app_center/error.log 2>/dev/null | tail -20

line "6. 能安装的参照物（装一个已知正常的第三方 fpk，用于区分是包的问题还是系统的问题）"
echo "把 FnBlackboard_0.3.12_x86.fpk 传到 NAS 上，执行："
echo "  sudo appcenter-cli install-fpk /tmp/FnBlackboard_0.3.12_x86.fpk -v 1"
echo "如果它也报 10238，说明系统侧装不了任何第三方 fpk，和我们的包无关。"

line "7. 那个 bad file number 的挂载点（日志里每 30 秒刷一次）"
P=/vol02/1000-1-50e96732/media
ls -ld "$P" 2>&1
stat -f "$P" 2>&1 | head -3
mount 2>/dev/null | grep -E 'media|1000-1-50e96732' | head -5
echo "（这条本身不是安装失败的直接原因，但说明有一个 bind 挂载已经失效，值得单独修）"

line "8. 结果怎么用"
echo "把第 1、3、4、5、6 节的原样输出贴回来即可。"
