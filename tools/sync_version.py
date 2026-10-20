#!/usr/bin/env python3
"""把远端的版本号同步回本地。

每日自动发版会把 manifest 的补丁号往前推，而本地仓库一直用 API 增量提交、
不与远端同步历史，所以本地版本号会慢慢落后。改代码前跑一次这个脚本，
本地构建出来的 fpk 版本号才和远端对得上。

用法：python3 tools/sync_version.py [--check]
    --check  只看差异，不写文件
"""
import base64
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "manifest")
SERVER = os.path.join(ROOT, "app", "server.py")
REPO = "cavemannb/fnos-upslink"


def local_field(name):
    with open(MANIFEST, encoding="utf-8") as f:
        m = re.search(r"^%s\s*=\s*(.*)$" % re.escape(name), f.read(), re.M)
    return m.group(1).strip() if m else "?"


def remote_manifest():
    """取远端 manifest 里的 version 与 changelog（自动发版会同时改这两个字段）。"""
    url = "https://api.github.com/repos/%s/contents/manifest" % REPO
    headers = {"User-Agent": "upslink-sync", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            with open(os.path.join(os.path.expanduser("~"), ".gh_token"), encoding="utf-8") as f:
                token = f.read().strip()
        except Exception:
            token = ""
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    text = base64.b64decode(data["content"]).decode("utf-8")
    out = {}
    for key in ("version", "changelog"):
        m = re.search(r"^%s\s*=\s*(.*)$" % key, text, re.M)
        out[key] = m.group(1).strip() if m else ""
    return out


def main():
    cur = local_field("version")
    try:
        remote = remote_manifest()
    except Exception as e:
        print("读取远端版本失败：%s" % e)
        return 1
    new = remote.get("version") or ""
    if not new or new == "?":
        print("远端 manifest 里没找到 version 字段")
        return 1

    print("版本：本地 %s → 远端 %s" % (cur, new))
    if new == cur:
        print("已是最新，无需改动。")
        return 0
    if "--check" in sys.argv:
        print("（--check：未写入）")
        return 0

    with open(MANIFEST, encoding="utf-8") as f:
        mf = f.read()
    for key in ("version", "changelog"):
        val = remote.get(key)
        if val:
            mf = re.sub(r"^(%s[ \t]*=[ \t]*).*$" % key,
                        lambda m, v=val: m.group(1) + v, mf, count=1, flags=re.M)
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
        f.write(mf)

    with open(SERVER, encoding="utf-8") as f:
        srv = f.read()
    srv = re.sub(r'APP_VERSION = "[^"]*"', 'APP_VERSION = "%s"' % new, srv, count=1)
    with open(SERVER, "w", encoding="utf-8", newline="\n") as f:
        f.write(srv)

    print("已写回 manifest 与 app/server.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
