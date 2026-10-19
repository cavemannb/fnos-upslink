#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 UPSLink 的四个图标（纯标准库，无第三方依赖）
   ICON.PNG(64) / ICON_256.PNG(256) / app/ui/images/icon-64.png / icon-256.png
   风格参照飞牛原生图标：蓝色渐变圆角超椭圆 + 白色闪电
"""
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = (71, 120, 255)    # #4778FF
BOTTOM = (61, 93, 255)  # #3D5DFF

BOLT = [
    (0.10, -0.50), (-0.30, 0.06), (-0.02, 0.06),
    (-0.12, 0.50), (0.30, -0.06), (0.02, -0.06),
]


def in_poly(px, py, pts):
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py):
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def render(size, ss=4):
    """以 ss 倍超采样渲染 size×size 的 RGBA 图，再盒式降采样做抗锯齿。"""
    big = size * ss
    grid = [[(0, 0, 0, 0)] * big for _ in range(big)]
    for y in range(big):
        ny = (y + 0.5) / big - 0.5
        t = ny + 0.5
        r = int(TOP[0] + (BOTTOM[0] - TOP[0]) * t)
        g = int(TOP[1] + (BOTTOM[1] - TOP[1]) * t)
        b = int(TOP[2] + (BOTTOM[2] - TOP[2]) * t)
        row = grid[y]
        for x in range(big):
            nx = (x + 0.5) / big - 0.5
            if abs(nx) ** 4 + abs(ny) ** 4 > 0.25 ** 2 * 1.0:
                continue
            if in_poly(nx, ny, BOLT):
                row[x] = (255, 255, 255, 255)
            else:
                row[x] = (r, g, b, 255)

    out = []
    for oy in range(size):
        line = []
        for ox in range(size):
            sr = sg = sb = 0
            opaque = 0
            total = 0
            for dy in range(ss):
                for dx in range(ss):
                    pr, pg, pb, pa = grid[oy * ss + dy][ox * ss + dx]
                    total += 1
                    if pa:
                        sr += pr
                        sg += pg
                        sb += pb
                        opaque += 1
            if opaque:
                a = int(255 * opaque / total)
                line.extend([sr // opaque, sg // opaque, sb // opaque, a])
            else:
                line.extend([0, 0, 0, 0])
        out.append(bytes(line))
    return out


def write_png(path, size, rows):
    raw = b"".join(b"\x00" + r for r in rows)
    comp = zlib.compress(raw, 9)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", comp) +
           chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    print("%-48s %d bytes" % (os.path.relpath(path, ROOT), len(png)))


if __name__ == "__main__":
    for size in (256, 64):
        rows = render(size, ss=4 if size >= 256 else 8)
        if size == 256:
            write_png(os.path.join(ROOT, "ICON_256.PNG"), size, rows)
            write_png(os.path.join(ROOT, "app", "ui", "images", "icon-256.png"), size, rows)
        else:
            write_png(os.path.join(ROOT, "ICON.PNG"), size, rows)
            write_png(os.path.join(ROOT, "app", "ui", "images", "icon-64.png"), size, rows)
