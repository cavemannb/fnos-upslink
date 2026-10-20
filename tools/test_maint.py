"""电池保养排期验证：不联网，用假趋势样本推进。

重点保证四件事：
  1. 短时放电会自动记成「一次短测」，30 秒以内的状态抖动不算；
  2. 放电放得太空会单独记一次「深度放电」并提醒——铅酸不能靠放空保养；
  3. 到期 / 逾期 / 重复扫描的账都算得对，同一段放电不会被数两遍；
  4. 保养只是建议，不改配置、不参与关机判断。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import server  # noqa: E402

tmp = tempfile.mkdtemp()
server.DATA_DIR = tmp
server.CONF_PATH = os.path.join(tmp, "config.json")
server.CALIB_PATH = os.path.join(tmp, "calibration.json")
server.BATTERY_PATH = os.path.join(tmp, "battery.json")
server.MAINT_PATH = os.path.join(tmp, "maintenance.json")
server.EVENT_PATH = os.path.join(tmp, "events.log")

NOW = int(time.time())
server_engine = server.Engine()
server_engine.conf = dict(server.DEFAULT_CONF)


def check(label, cond, extra=""):
    if not cond:
        raise AssertionError("%s 失败 %s" % (label, extra))
    print("  OK  %s %s" % (label, extra))


def reset():
    if os.path.isfile(server.MAINT_PATH):
        os.remove(server.MAINT_PATH)


def trend(points):
    class T:
        pass
    t = T()
    t.samples = sorted(points, key=lambda p: p["t"])
    return t


def pt(ts, charge, status="OL", load=11, runtime=1800):
    return {"t": int(ts), "c": charge, "l": load, "r": runtime, "s": status}


def event_text():
    return " | ".join(str(e.get("msg", "")) for e in server_engine.events.recent(20))


print("1. 全新安装：给出建议间隔，不催")
reset()
m = server.maintenance_status(trend([]))
check("等级 ok", m["level"] == "ok", m["level"])
check("短测 30 天后", m["short"]["due_in_days"] == 30, str(m["short"]["due_in_days"]))
check("标定 180 天后", m["calib"]["due_in_days"] == 180, str(m["calib"]["due_in_days"]))
check("文案提示未记录", "还没有维护记录" in m["message"], m["message"])

print("2. 150 秒的放电自动记成一次短测")
pts = [pt(NOW - 2000, 100), pt(NOW - 1800, 100, "OB"),
       pt(NOW - 1650, 99, "OB"), pt(NOW - 1649, 99)]
m = server.maintenance_status(trend(pts), server_engine.events)
check("记了一次", m["short"]["count"] == 1, str(m["short"]["count"]))
check("上次就在刚才", m["short"]["last_days"] == 0, str(m["short"]["last_days"]))
check("排期重置（约 30 天后）", m["short"]["due_in_days"] in (29, 30), str(m["short"]["due_in_days"]))
check("事件里有记录", "电池短测" in event_text())

print("3. 10 秒的状态抖动不算一次放电")
pts += [pt(NOW - 1600, 99), pt(NOW - 1500, 99, "OB"), pt(NOW - 1490, 99)]
m = server.maintenance_status(trend(pts), server_engine.events)
check("仍然只有一次", m["short"]["count"] == 1, str(m["short"]["count"]))

print("4. 放到 35% 记一次深度放电")
pts += [pt(NOW - 1400, 98), pt(NOW - 1300, 60, "OB"),
        pt(NOW - 1200, 35, "OB"), pt(NOW - 1199, 35)]
m = server.maintenance_status(trend(pts), server_engine.events)
check("深度放电 1 次", m["deep_count"] == 1, str(m["deep_count"]))
check("事件里有警告", "深度放电" in event_text())

print("5. 11.7 分钟、降 5% 的放电同时刷新容量样本")
pts += [pt(NOW - 1000, 95), pt(NOW - 900, 95, "OB"),
        pt(NOW - 200, 90, "OB"), pt(NOW - 199, 90)]
m = server.maintenance_status(trend(pts), server_engine.events)
check("标定排期重置（约 180 天后）", m["calib"]["due_in_days"] in (179, 180), str(m["calib"]["due_in_days"]))
check("上次标定是今天", m["calib"]["last_days"] == 0, str(m["calib"]["last_days"]))

print("6. 重复扫描不会重复计数")
m2 = server.maintenance_status(trend(pts), server_engine.events)
check("短测次数不变", m2["short"]["count"] == m["short"]["count"], str(m2["short"]["count"]))
check("深度放电次数不变", m2["deep_count"] == m["deep_count"], str(m2["deep_count"]))

print("7. 超过 30 天没短测 → 逾期")
state = json.load(open(server.MAINT_PATH, encoding="utf-8"))
state["last_short_at"] = NOW - 40 * 86400
json.dump(state, open(server.MAINT_PATH, "w", encoding="utf-8"))
m = server.maintenance_status(trend(pts))
check("等级 due", m["level"] == "due", m["level"])
check("提示短测", "短测" in m["message"], m["message"])
check("逾期 10 天", m["short"]["due_in_days"] == -10, str(m["short"]["due_in_days"]))
check("文案写明已过计划日期", "已过计划日期 10 天" in m["message"], m["message"])

print("8. 逾期只报事实，没有「推迟」这条路")
m = server.maintenance_status(trend(pts))
check("仍是 due，不会自动降级", m["level"] == "due", m["level"])
check("文案不含推迟字样", "推迟" not in m["message"], m["message"])
check("不再返回 snoozed_until", "snoozed_until" not in m, str(sorted(m.keys())))

print("9. 半年没标定 → 先催标定")
state["last_short_at"] = NOW - 86400
state["last_calib_at"] = NOW - 200 * 86400
json.dump(state, open(server.MAINT_PATH, "w", encoding="utf-8"))
m = server.maintenance_status(trend(pts))
check("等级 due", m["level"] == "due", m["level"])
check("提示标定", "容量标定" in m["message"], m["message"])
check("文案写明已过计划日期", "已过计划日期" in m["message"], m["message"])

print("10. recent 列表由历史自动汇总（无需手动报数）")
check("至少 2 条", len(m["recent"]) >= 2, str(len(m["recent"])))
check("最新在最前", m["recent"][0]["at"] >= m["recent"][-1]["at"],
      str([r["at"] for r in m["recent"]]))
check("每条带天数与时长", all(("days" in r and "sec" in r) for r in m["recent"]),
      str(m["recent"][:1]))

print("11. 保养不碰关机策略")
server_engine.refresh_maintenance(force=True)
eff, active = server_engine.effective_onbatt()
check("阈值仍是配置里的值", active is False, str(active))
check("配置未被改写", server_engine.conf["onbatt_seconds"] == server.DEFAULT_CONF["onbatt_seconds"],
      str(server_engine.conf["onbatt_seconds"]))

print("\n全部通过。")
