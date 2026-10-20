#!/usr/bin/env python3
"""自动发版用的版本号递增器：只由 .github/workflows/release.yml 调用。

做的事：
  1. 以「最近一次已发布的 tag」为基准把 patch +1（本地已经手动升过则沿用，不叠两次）
  2. app/server.py 的 APP_VERSION 同步（页面底部与 /api/state 都读它）
  3. manifest 的 changelog 字段改成一句话（飞牛安装时显示）
  4. 生成 release-notes.md 给 gh release create 用

不改 CHANGELOG.md：那个文件是人工维护的功能说明，自动发版每天往里写会跟本地改动打架，
自动版本的说明放 Release 正文就够了。
本地不要手动跑、也不要手动改 manifest 的 version：版本号只由发版流程推进。
之前每次「本地先 +1、CI 再 +1」，导致发布的版本号一直 +2（1.9.6 → 1.9.8 → 1.9.10），
现在由 next_version() 兜住这种情况。同步远端版本请用 tools/sync_version.py。
"""
import datetime as dt
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "manifest")
SERVER = os.path.join(ROOT, "app", "server.py")
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


def ver_tuple(ver):
    nums = [int(p) if p.isdigit() else 0 for p in str(ver).split(".")]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def latest_tag():
    """最近一次**已发布**的版本（tag），没有则 None。"""
    try:
        out = subprocess.run(["git", "tag", "--list", "v*", "--sort=-v:refname"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    for ln in out.splitlines():
        s = ln.strip().lstrip("v")
        if re.fullmatch(r"\d+\.\d+\.\d+", s):
            return s
    return None


def next_version(manifest_ver, tag_ver):
    """算下一个要发布的版本号。

    基准是「最近一次已发布的 tag」而不是 manifest：
    如果提交里已经手动把 manifest 升过（比 tag 新），就直接沿用，
    不再叠一次 —— 否则每个版本会稳定 +2（1.9.6 → 1.9.8 → 1.9.10）。
    """
    if tag_ver and ver_tuple(manifest_ver) > ver_tuple(tag_ver):
        return manifest_ver, "沿用提交里已升过的版本"
    base = max(ver_tuple(manifest_ver), ver_tuple(tag_ver or "0.0.0"))
    return bump("%d.%d.%d" % base), "在最近发布版本上 +1"


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
    tag = latest_tag()
    new, why = next_version(old, tag)
    head = commits[0] if commits else "例行构建"

    # manifest：版本号 + 一句话 changelog（飞牛安装弹窗里显示）
    # 提交信息常带「1.9.5：」这类版本前缀，去掉再拼，避免出现「1.9.6：1.9.5：…」
    short = re.sub(r"^v?\d+(?:\.\d+)*[:：]\s*", "", head)
    short = short if len(short) <= 40 else short[:40] + "…"
    mf = set_field(mf, "version", new)
    mf = set_field(mf, "changelog", "%s：%s" % (new, short))
    write(MANIFEST, mf)

    # server.py：页面底部与接口返回的版本号
    srv = read(SERVER)
    srv = re.sub(r'APP_VERSION = "[^"]*"', 'APP_VERSION = "%s"' % new, srv, count=1)
    write(SERVER, srv)

    # Release 说明
    items = commits or ["例行构建，无功能性改动。"]
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

    print("version %s -> %s（%s；最近发布 %s，%d 条代码提交）" % (
        old, new, why, tag or "无", len(commits)))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("ver=%s\n" % new)
            f.write("prev=%s\n" % old)


if __name__ == "__main__":
    main()
