#!/usr/bin/env python3
"""自动发版用的版本号递增器：只由 .github/workflows/release.yml 调用。

做的事：
  1. manifest 的 version 末位 +1（patch 满 100 进位到 minor，避免 1.9.137 这种版本）
  2. app/server.py 的 APP_VERSION 同步（页面底部与 /api/state 都读它）
  3. manifest 的 changelog 字段改成一句话（飞牛安装时显示）
  4. CHANGELOG.md 顶部插入当天的自动条目
  5. 生成 release-notes.md 给 gh release create 用
本地不要手动跑，否则版本号会跳到远端前面。
"""
import datetime as dt
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "manifest")
SERVER = os.path.join(ROOT, "app", "server.py")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
NOTES = os.path.join(ROOT, "release-notes.md")
CST = dt.timezone(dt.timedelta(hours=8))

# 只有这些路径的改动才算「代码改动」，文档与截图更新不触发发版
CODE_PATHS = ["app", "tools", "build-fpk.sh", "manifest", ".github"]
BOT_SUBJECT = "chore(release):"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def field(text, name):
    m = re.search(r"^%s\s*=\s*(.*)$" % re.escape(name), text, re.M)
    return m.group(1).strip() if m else ""


def set_field(text, name, value):
    """保留 `key` 到 `=` 之间的对齐空格，只换值。"""
    return re.sub(r"^(%s[ \t]*=[ \t]*).*$" % re.escape(name),
                  lambda m: m.group(1) + value, text, count=1, flags=re.M)


def bump(ver):
    parts = ver.split(".")
    while len(parts) < 3:
        parts.append("0")
    nums = [int(p) if p.isdigit() else 0 for p in parts[:3]]
    nums[2] += 1
    if nums[2] >= 100:          # patch 满 100 进一位，版本号别太长
        nums[1] += 1
        nums[2] = 0
    if nums[1] >= 10:
        nums[0] += 1
        nums[1] = 0
    return "%d.%d.%d" % (nums[0], nums[1], nums[2])


def day_commits():
    """过去 24 小时的代码提交（排除本工作流自己产生的版本号回写）。"""
    try:
        out = subprocess.run(
            ["git", "log", "--since=24 hours ago", "--no-merges",
             "--pretty=format:%s", "--invert-grep", "--grep=^" + BOT_SUBJECT,
             "--"] + CODE_PATHS,
            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def main():
    if "--check" in sys.argv:
        # 只判断「今天有没有值得发版的代码改动」，供工作流决定是否往下走
        changed = bool(day_commits())
        print("changed" if changed else "no-change")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write("has=%s\n" % ("true" if changed else "false"))
        return

    today = dt.datetime.now(CST).strftime("%Y-%m-%d")
    commits = day_commits()

    mf = read(MANIFEST)
    old = field(mf, "version")
    new = bump(old)
    head = commits[0] if commits else "例行构建"

    # manifest：版本号 + 一句话 changelog（飞牛安装弹窗里显示）
    short = head if len(head) <= 40 else head[:40] + "…"
    mf = set_field(mf, "version", new)
    mf = set_field(mf, "changelog", "%s：%s" % (new, short))
    write(MANIFEST, mf)

    # server.py：页面底部与接口返回的版本号
    srv = read(SERVER)
    srv = re.sub(r'APP_VERSION = "[^"]*"', 'APP_VERSION = "%s"' % new, srv, count=1)
    write(SERVER, srv)

    # CHANGELOG：顶部插入当天条目
    items = commits or ["例行构建，无功能性改动。"]
    entry = "## %s · %s（自动发布）\n\n%s\n\n" % (
        new, today, "\n".join("- " + c for c in items))
    log = read(CHANGELOG)
    marker = "# 更新日志\n\n"
    assert marker in log, "CHANGELOG.md 结构变了，找不到标题行"
    write(CHANGELOG, log.replace(marker, marker + entry, 1))

    # Release 说明
    write(NOTES, "\n".join([
        "## 自动发布 · %s" % today,
        "",
        "过去 24 小时的代码改动：",
        "",
    ] + ["- " + c for c in items] + [
        "",
        "**安装**：下载下方 `upslink-%s.fpk`，飞牛应用中心 → 手动安装 → 上传即可覆盖旧版本。" % new,
        "",
        "**说明**：本版本由每天 09:00 的定时任务自动构建；没有代码改动的日子不会发版。",
    ]) + "\n")

    print("version %s -> %s（%d 条代码提交）" % (old, new, len(commits)))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("ver=%s\n" % new)
            f.write("prev=%s\n" % old)


if __name__ == "__main__":
    main()
