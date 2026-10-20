"""电池健康逻辑的单元验证（不依赖真机，直接喂构造样本）。"""
import os
import sys
import tempfile
import time
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import server  # noqa: E402


class FakeTrend:
    def __init__(self, samples):
        self.samples = samples


def outage(start, minutes, c0, c1, load=11, step=2):
    pts = []
    n = int(minutes / step)
    for i in range(n + 1):
        pts.append({"t": start + i * step * 60,
                    "c": round(c0 + (c1 - c0) * i / float(n), 2),
                    "l": load, "r": 1200, "s": "OB"})
    return pts


def online(start):
    return [{"t": start, "c": 100, "l": 11, "r": 1200, "s": "OL"}]


def check(label, cond, extra=""):
    if not cond:
        raise AssertionError("%s 失败 %s" % (label, extra))
    print("  OK  %s %s" % (label, extra))


tmp = tempfile.mkdtemp()
server.BATTERY_PATH = os.path.join(tmp, "battery.json")
server.EVENT_PATH = os.path.join(tmp, "events.log")
now = time.time()

print("1. 日期解析与月份加法")
check("2026-10-15", server._parse_date("2026-10-15") == datetime.date(2026, 10, 15))
check("斜杠写法", server._parse_date("2026/10/15") == datetime.date(2026, 10, 15))
check("空值", server._parse_date("") is None)
check("非法日期", server._parse_date("2026-13-01") is None)
check("24 个月", server._add_months(datetime.date(2026, 10, 15), 24) == datetime.date(2028, 10, 15))
check("月末夹取", server._add_months(datetime.date(2026, 1, 31), 1) == datetime.date(2026, 2, 28))

print("2. 未登记投运日期")
b = server.battery_health({"battery_installed": ""}, {}, FakeTrend([]))
check("level=unknown", b["level"] == "unknown", b["message"])

print("3. 刚换的电池（2026-10-15 / 24 个月）")
b = server.battery_health({"battery_installed": "2026-10-15", "battery_life_months": 24},
                          {"battery.charge": "100"}, FakeTrend([]))
check("level=ok", b["level"] == "ok", b["message"])
check("剩余天数 > 700", b["days_left"] > 700, "days_left=%s" % b["days_left"])

print("4. 已超期")
b = server.battery_health({"battery_installed": "2024-01-01", "battery_life_months": 24},
                          {}, FakeTrend([]))
check("level=due", b["level"] == "due", b["message"])

print("5. 实测容量：首断电记基线，第二次同水平不报警")
samples = outage(now - 7200, 20, 100, 90)
samples += online(now - 6000)
samples += outage(now - 5000, 20, 100, 90)
samples += online(now - 3700)
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"}, FakeTrend(samples))
check("测得满电时长（到 20%）", b["measured_minutes"] and 150 < b["measured_minutes"] < 170, "%.0f 分钟" % b["measured_minutes"])
check("外推到 0% 更长", b["measured_minutes0"] > b["measured_minutes"] * 1.2, "%.0f 分钟" % b["measured_minutes0"])
check("记下样本负载", b["measured_load"] == 11, b["measured_load"])
check("基线已建立", b["baseline_minutes"] is not None)
check("记录基线负载", b["baseline_load"] == 11, b["baseline_load"])
check("无衰减时 level=ok", b["level"] == "ok", b["message"])
check("两次样本", b["samples"] == 2, b["samples"])

print("6. 实测容量明显衰减（同样 20 分钟掉 20%）")
server.BATTERY_PATH = os.path.join(tmp, "battery2.json")
samples = outage(now - 7200, 20, 100, 90) + online(now - 6000)
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"}, FakeTrend(samples))
base = b["baseline_minutes"]
samples += outage(now - 3000, 20, 100, 80) + online(now - 1800)
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"}, FakeTrend(samples))
check("检测到衰减", b["decay_pct"] and b["decay_pct"] >= 30, "decay=%.0f%%" % (b["decay_pct"] or 0))
check("level=due", b["level"] == "due", b["message"])

print("7. 负载不同不误判成衰减")
server.BATTERY_PATH = os.path.join(tmp, "battery3.json")
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"},
                          FakeTrend(outage(now - 3600, 20, 100, 90, load=11) + online(now - 2400)))
check("单样本按样本负载折算", 155 < b["measured_minutes"] < 165, "%.0f 分钟" % b["measured_minutes"])
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "22"},
                          FakeTrend(outage(now - 1200, 10, 100, 90, load=22) + online(now - 600)))
check("负载翻倍 → 时长减半", 75 < b["measured_minutes"] < 85, "%.0f 分钟" % b["measured_minutes"])
check("容量没变 → 不算衰减", (b["decay_pct"] or 0) < 3, "decay=%s" % b["decay_pct"])
check("level 仍 ok", b["level"] == "ok", b["message"])

print("8. 短断电样本被丢弃")
short = outage(now - 900, 4, 100, 99)
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"}, FakeTrend(short))
check("样本不足", b["measured_minutes"] is None and b["samples"] == 0)

print("9. 同一批样本重复取数必须稳定（负载在跳也不该变）")
server.BATTERY_PATH = os.path.join(tmp, "battery4.json")
samples = outage(now - 7200, 20, 100, 90) + online(now - 6000)
server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"}, FakeTrend(samples))
vals = set()
for lv in ("8", "11", "17", "23", "9"):
    b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": lv}, FakeTrend(samples))
    vals.add(b["measured_minutes"])
check("负载波动不影响已测得的容量", len(vals) == 1, sorted(vals))

print("10. 还在放电的那一段不计入（时长每分钟都在变长）")
open_run = outage(now - 600, 9, 100, 96)
b = server.battery_health({"battery_installed": "2026-10-15"}, {"ups.load": "11"}, FakeTrend(open_run))
check("未恢复市电 → 不给实测值", b["measured_minutes"] is None and b["samples"] == 0)

print("\n全部通过。")
