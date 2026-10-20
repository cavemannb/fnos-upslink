"""「上次断电」这条信息的来源判定（短测 / 容量标定 / 真断电）。

不连真机，直接喂构造的趋势样本。重点验证两件事：
  1. 短测和标定不能冒充「真断电」把总览卡那条信息顶掉；
  2. 90 秒这种短放电的时长要算得出来（以前只有一个 OB 采样点，算成 0 秒）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import server  # noqa: E402

TMP = tempfile.mkdtemp(prefix="upslink-outage-")
server.CALIB_LOG_PATH = os.path.join(TMP, "calib-windows.json")

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  OK   %s%s" % (label, (" · " + str(detail)) if detail != "" else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" · " + str(detail)) if detail != "" else ""))


class FakeTrend:
    def __init__(self, samples):
        self.samples = samples


def ob(start, sec, c0=100, c1=96, load=11):
    """一段电池供电的 OB 采样点（不含恢复点）。"""
    pts = [{"t": start, "c": c0, "l": load, "s": "OB"}]
    if sec >= 120:
        pts.append({"t": start + sec // 2, "c": round((c0 + c1) / 2.0), "l": load, "s": "OB"})
    return pts


def ol(at, c=100):
    return [{"t": at, "c": c, "l": 11, "s": "OL"}]


def runs_of(samples):
    return server.ob_runs(FakeTrend(samples))


print("1. 90 秒短测：时长算得出来，且归类为短测")
t = 1700000000
runs = runs_of(ol(t - 300) + ob(t, 90, 100, 99) + ol(t + 90, 99))
check("只有一段区间", len(runs) == 1, len(runs))
check("时长 90 秒（不是 0）", runs[0]["seconds"] == 90, runs[0]["seconds"])
check("恢复时刻 = 恢复市电的采样", runs[0]["at"] == t + 90, runs[0]["at"] - t)
check("归类 short/test", server.classify_discharge(runs[0], []) == "test")
check("summary 也标 test", server.outage_summary(runs, [])["kind"] == "test")

print("\n2. 20 秒瞬断归为 blip")
runs = runs_of(ol(t - 300) + ob(t, 20, 100, 100) + ol(t + 20))
check("归类 blip", server.classify_discharge(runs[0], []) == "blip")

print("\n3. 20 分钟真断电归为 outage")
runs = runs_of(ol(t - 300) + ob(t, 1200, 100, 90) + ol(t + 1200, 90))
check("时长 1200 秒", runs[0]["seconds"] == 1200, runs[0]["seconds"])
check("归类 outage", server.classify_discharge(runs[0], []) == "outage")

print("\n4. 短测不能顶掉「上次断电」")
samples = (ol(t - 5000)
           + ob(t - 4000, 1200, 100, 90) + ol(t - 2800, 90)      # 真断电：20 分钟
           + ob(t - 600, 90, 100, 99) + ol(t - 510, 99))         # 之后做了短测
runs = runs_of(samples)
s = server.outage_summary(runs, [])
check("显示的是真断电", s["kind"] == "outage")
check("时刻是真断电的结束时刻", s["at"] == t - 2800, s["at"] - t)
check("时长是 20 分钟", s["seconds"] == 1200, s["seconds"])
check("最新一次电池供电作为附注保留", s.get("latest", {}).get("kind") == "test", s.get("latest"))
check("附注时长 90 秒", s.get("latest", {}).get("seconds") == 90)

print("\n5. 只有短测时仍然显示（但标明来源，不当断电）")
s = server.outage_summary(runs_of(ol(t - 300) + ob(t, 90, 100, 99) + ol(t + 90, 99)), [])
check("返回 test", s["kind"] == "test")
check("没有附注", "latest" not in s, s.get("latest"))

print("\n6. 标定期间的放电归为 calib（哪怕超过 8 分钟）")
windows = [{"start": t - 60, "end": t + 900, "grace_until": 0}]
s = server.outage_summary(runs_of(ol(t - 300) + ob(t, 600, 100, 92) + ol(t + 600, 92)), windows)
check("归类 calib", s["kind"] == "calib", s["kind"])
check("恰好 10 分钟也不算断电", s["seconds"] == 600)

print("\n7. 标定结束后宽限期内继续放电，仍算标定")
windows = [{"start": t - 60, "end": t - 30, "grace_until": t + 120}]
s = server.outage_summary(runs_of(ol(t - 300) + ob(t, 600, 100, 92) + ol(t + 600, 92)), windows)
check("归类 calib", s["kind"] == "calib", s["kind"])

print("\n8. 标定窗口之外的长放电照常算断电")
windows = [{"start": t - 9000, "end": t - 8000, "grace_until": 0}]
s = server.outage_summary(runs_of(ol(t - 300) + ob(t, 600, 100, 92) + ol(t + 600, 92)), windows)
check("归类 outage", s["kind"] == "outage", s["kind"])

print("\n9. 正在放电时按断电算（还不知道最终多长）")
runs = runs_of(ol(t - 300) + ob(t, 300, 100, 97))
check("open 标记", runs[0]["open"] is True)
check("归类 outage", server.classify_discharge(runs[0], []) == "outage")
s = server.outage_summary(runs, [])
check("summary ongoing", s["ongoing"] is True and s["kind"] == "outage")

print("\n10. 标定窗口流水：写入 / 收口 / 判定")
server.calib_log_start(t)
windows = server._load_calib_log()
check("写入一条窗口", len(windows) == 1 and windows[0]["start"] == t, windows)
check("窗口内算标定", server.in_calib_window(t + 300) is True)
server.calib_log_end(t + 600, t + 780)
windows = server._load_calib_log()
check("收口写入结束时刻", windows[0]["end"] == t + 600, windows[0])
check("宽限期内仍算标定", server.in_calib_window(t + 700) is True)
check("宽限过期后不算", server.in_calib_window(t + 780 + server.CALIB_WINDOW_SLACK + 10) is False)
check("窗口之前不算", server.in_calib_window(t - 600) is False)

print("\n11. 没有任何放电记录")
check("返回 None", server.outage_summary([], []) is None)

print("\n%d 项通过，%d 项失败。" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
