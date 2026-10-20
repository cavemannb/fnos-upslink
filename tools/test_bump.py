"""发版版本号的计算规则（不联网、不碰仓库，只喂数字）。

重点回归「版本号每次 +2」这个 bug：本地先把 manifest 升了 1，CI 又升 1。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bump_version as bv  # noqa: E402


def check(label, got, want):
    if got != want:
        raise AssertionError("%s：得到 %r，期望 %r" % (label, got, want))
    print("  OK  %s = %s" % (label, got))


print("1. 末位递增")
check("补丁号 +1", bv.bump("1.9.12"), "1.9.13")
check("补 0", bv.bump("1.9"), "1.9.1")
check("patch 满 100 进位", bv.bump("1.9.98"), "1.9.99")
check("patch 100 → 2.0.0（minor 不允许到 10）", bv.bump("1.9.99"), "2.0.0")

print("2. 正常情况：manifest 停在最近发布的版本上 → +1")
check("1.9.12 / tag 1.9.12", bv.next_version("1.9.12", "1.9.12")[0], "1.9.13")

print("3. 提交里已经手动升过版本 → 沿用，不再 +1")
check("manifest 1.9.13 / tag 1.9.12", bv.next_version("1.9.13", "1.9.12")[0], "1.9.13")
check("提示语", bv.next_version("1.9.13", "1.9.12")[1], "沿用提交里已升过的版本")

print("4. 本地落后于远端（忘了 sync）→ 以 tag 为准，不回退")
check("manifest 1.8.0 / tag 1.9.12", bv.next_version("1.8.0", "1.9.12")[0], "1.9.13")

print("5. 还没有任何 tag（首次发版）")
check("manifest 1.2.0 / 无 tag", bv.next_version("1.2.0", None)[0], "1.2.1")

print("6. 版本比较不要按字符串比")
check("1.10.0 > 1.9.9", bv.ver_tuple("1.10.0") > bv.ver_tuple("1.9.9"), True)
check("1.9.10 > 1.9.9", bv.ver_tuple("1.9.10") > bv.ver_tuple("1.9.9"), True)

print("\n全部通过。")
