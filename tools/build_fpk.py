#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按官方 fnpack 1.2.3 的产物格式打 .fpk（不依赖 fnpack，也不需要 Linux）。

参照物：一个真机可正常安装的第三方 fpk（fnblackboard 0.3.12）解包后的实际结构：
    app.tgz
    cmd/…（9 个生命周期脚本）
    config/privilege, config/resource
    ICON.PNG, ICON_256.PNG
    manifest
    wizard/uninstall

关键约定（踩过的坑都在这）：
  1. 归档内条目**不能带 "./" 前缀**，必须叫 manifest / cmd/main / app.tgz；
  2. manifest.checksum 必须等于 app.tgz 字节内容的 MD5（1.0.1 起已补，报错是不带文本的 10238）；
  3. 只允许上述顶层条目，多一个根级 ui/ 这种非官方目录属于自找麻烦；
  4. 生命周期脚本必须容错：安装期脚本返回非 0 会直接把安装打断。
"""

import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")

# 固定时间戳，保证同样内容打出的包字节稳定
FIXED_MTIME = 1760000000


def read_manifest():
    path = os.path.join(ROOT, "manifest")
    fields = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh.read().replace("\r\n", "\n").split("\n"):
            if not line.strip() or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields.append((key.strip(), value.strip()))
    return fields


def manifest_text(fields):
    # 官方产物是 key 左对齐 27 列 + "= " + 值，这里保持一致
    return "".join("%-27s= %s\n" % (k, v) for k, v in fields)


def add_file(tf, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = FIXED_MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tf.addfile(info, io.BytesIO(data))


def add_dir(tf, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = FIXED_MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    tf.addfile(info)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def build_app_tgz():
    """app/ 下的内容 → app.tgz（安装后解到 target/）"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        entries = []
        app_dir = os.path.join(ROOT, "app")
        for base, dirs, files in os.walk(app_dir):
            dirs.sort()
            rel_base = os.path.relpath(base, app_dir).replace("\\", "/")
            if rel_base != ".":
                entries.append(("d", rel_base, os.path.join(base, "")))
            for name in sorted(files):
                if name.endswith(".pyc") or "__pycache__" in base:
                    continue
                rel = name if rel_base == "." else rel_base + "/" + name
                entries.append(("f", rel, os.path.join(base, name)))
        # 目录先建，文件后加，解包顺序稳妥
        for kind, rel, path in entries:
            if kind == "d":
                add_dir(tf, rel)
            else:
                mode = 0o755 if rel.endswith(".py") else 0o644
                add_file(tf, rel, read(path), mode)
    raw = buf.getvalue()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(raw)
    return out.getvalue()


def build():
    fields = read_manifest()
    meta = dict(fields)
    appname, version = meta["appname"], meta["version"]

    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(DIST, exist_ok=True)

    app_tgz = build_app_tgz()
    checksum = hashlib.md5(app_tgz).hexdigest()

    # 回填 checksum
    fields = [(k, checksum if k == "checksum" else v) for k, v in fields]
    mtext = manifest_text(fields).encode("utf-8")

    out_path = os.path.join(DIST, "%s-%s.fpk" % (appname, version))
    if os.path.exists(out_path):
        os.remove(out_path)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        # 顺序与官方产物一致
        add_file(tf, "app.tgz", app_tgz)
        add_dir(tf, "cmd")
        for name in sorted(os.listdir(os.path.join(ROOT, "cmd"))):
            add_file(tf, "cmd/" + name, read(os.path.join(ROOT, "cmd", name)), 0o755)
        add_dir(tf, "config")
        for name in ("privilege", "resource"):
            add_file(tf, "config/" + name, read(os.path.join(ROOT, "config", name)))
        add_file(tf, "ICON.PNG", read(os.path.join(ROOT, "ICON.PNG")))
        add_file(tf, "ICON_256.PNG", read(os.path.join(ROOT, "ICON_256.PNG")))
        add_file(tf, "manifest", mtext)
        add_dir(tf, "wizard")
        for name in sorted(os.listdir(os.path.join(ROOT, "wizard"))):
            add_file(tf, "wizard/" + name, read(os.path.join(ROOT, "wizard", name)))

    raw = buf.getvalue()
    with open(out_path, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(raw)

    # ---- 自检 ----
    with tarfile.open(out_path, "r:gz") as tf:
        names = tf.getnames()
        got = tf.extractfile("manifest").read().decode("utf-8")
        inner = tf.extractfile("app.tgz").read()

    real = hashlib.md5(inner).hexdigest()
    stated = dict(
        (k.strip(), v.strip())
        for k, _, v in (l.partition("=") for l in got.splitlines() if "=" in l)
    ).get("checksum", "")

    problems = []
    if real != stated:
        problems.append("checksum 不一致：manifest=%s 实际=%s" % (stated, real))
    for required in ("manifest", "app.tgz", "ICON.PNG", "ICON_256.PNG"):
        if required not in names:
            problems.append("缺少 %s" % required)
    bad = [n for n in names if n.startswith("./") or n.startswith("/")]
    if bad:
        problems.append("条目带 ./ 或 / 前缀：%s" % bad[:3])
    extra = set(n.split("/")[0] for n in names) - {
        "manifest", "app.tgz", "ICON.PNG", "ICON_256.PNG", "cmd", "config", "wizard"}
    if extra:
        problems.append("出现非官方顶层条目：%s" % sorted(extra))
    for need in ("cmd/main", "cmd/install_init", "cmd/install_callback",
                 "config/privilege", "config/resource"):
        if need not in names:
            problems.append("缺少 %s" % need)

    print("appname      : %s" % appname)
    print("version      : %s" % version)
    print("app.tgz md5  : %s" % real)
    print("manifest     : %s" % ("一致" if real == stated else "不一致"))
    print("文件名       : %s" % os.path.basename(out_path))
    print("体积         : %.1f KB" % (os.path.getsize(out_path) / 1024.0))
    print("条目         : %s" % ", ".join(names))

    if problems:
        print("\n自检失败：")
        for p in problems:
            print("  - " + p)
        return 1
    print("\n自检通过：结构、checksum、顶层条目均符合官方产物格式。")
    return 0


if __name__ == "__main__":
    sys.exit(build())
