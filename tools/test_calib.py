"""容量标定的状态机验证：不联网、不碰真机，用假的 vars 推进。

重点保证四件事：
  1. 标定期间「断电计时」用的是抬高后的阈值，配置文件本身一个字都不改；
  2. 到点 / 低电量 / FSD 都能把状态收回去，收回去之后原策略立刻恢复；
  3. 采到样本就写进实测容量，采不到就明说原因，不谎报；
  4. 进程重启后不存在「阈值被留在 20 分钟」的残留。
"""
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
server.EVENT_PATH = os.path.join(tmp, "events.log")

eng = server.Engine()
eng.conf = dict(server.DEFAULT_CONF)
eng.conf.update({"host": "192.168.1.20", "port": 3493, "ups": "ups",
                 "onbatt_seconds": 300, "auto_shutdown": True})


def check(label, cond, extra=""):
    if not cond:
        raise AssertionError("%s 失败 %s" % (label, extra))
    print("  OK  %s %s" % (label, extra))


def msgs(n=4):
    return [e.get("msg", "") for e in eng.events.recent(n)]


def ob(charge=96, status="OB", low="20"):
    return {"ups.status": status, "battery.charge": str(charge),
            "battery.charge.low": low, "ups.load": "11"}


print("1. 未配置上游时拒绝启动")
eng.conf["host"] = ""
ok, msg = eng.calib_start(15)
check("拒绝", ok is False and "上游" in msg, msg)
eng.conf["host"] = "192.168.1.20"

print("2. 启动后阈值抬高，配置文件不受影响")
ok, msg = eng.calib_start(15)
check("启动成功", ok is True, msg)
eff, active = eng.effective_onbatt()
check("生效阈值 1200 秒", eff == 1200 and active is True, "eff=%d" % eff)
check("配置仍是 300 秒", eng.conf["onbatt_seconds"] == 300, str(eng.conf["onbatt_seconds"]))
check("窗口 15 分钟", eng.calib["window"] == 15)
check("建议放电时长在 8~12 分钟内", 8 <= eng.calib["hint"] <= 12, str(eng.calib["hint"]))
check("没读到续航时给保守值", eng.calib["hint"] == 10, str(eng.calib["hint"]))
check("状态文件已落盘", os.path.isfile(server.CALIB_PATH))

print("3. 已断电 400 秒：不关机（原阈值 300 秒早该关了）")
eng.vars = ob()
eng.prev_status = "OL"
eng.onbatt_since = time.time() - 400
eng.evaluate(drill=True)
check("没有触发关机", not any("本应关机" in m for m in msgs(4)), str(msgs(4)))

print("4. 结束标定但还在放电：先宽限，不立刻关机")
eng.calib_finish("单元测试")
check("状态已收", eng.calib is None)
wait, _ = eng.effective_onbatt()
check("宽限期内阈值被抬高", wait > 300, str(wait))
check("宽限窗口还在", eng.calib_grace_until > time.time())
eng.evaluate(drill=True)
check("宽限期内不关机", not any("本应关机" in m for m in msgs(3)), str(msgs(3)))
eng.calib_grace_until = int(time.time()) - 1
eng.evaluate(drill=True)
check("宽限结束才关机", any("未恢复市电" in m for m in msgs(3)), str(msgs(3)))
eng.shutting_down = False
eng.calib_grace_until = 0

print("4b. 已插回市电时结束：阈值立刻回到 300")
eng.onbatt_since = None
eng.vars = ob(status="OL")
eng.calib_start(15)
eng.calib_finish("单元测试")
check("没有宽限", eng.calib_grace_until == 0)
check("阈值回到 300", eng.effective_onbatt()[0] == 300)

print("5. 到点自动结束")
eng.onbatt_since = None
eng.vars = ob(status="OL")
ok, _ = eng.calib_start(6)
check("窗口 6 分钟", ok is True and eng.calib["window"] == 6)
eng.calib["until"] = time.time() - 1
eng.calib_check(ob(status="OL"))
check("自动结束", eng.calib is None)
check("结束事件", any("标定结束（窗口结束）" in m for m in msgs(2)), str(msgs(2)))

print("6. 电量过低立即中止")
eng.calib_start(15)
eng.vars = ob(charge=45)
eng.calib_check(eng.vars)
check("已中止", eng.calib is None)
check("原因是电量", any("电量降到 45% 保护线" in m for m in msgs(2)), str(msgs(2)))
check("中止后给宽限", eng.calib_grace_until > time.time())
eng.calib_grace_until = 0
check("没有样本时不谎报", eng.calib_last is not None and eng.calib_last["ok"] is False)

print("7. FSD 也要能中止")
eng.calib_start(15)
eng.vars = {"ups.status": "OB FSD", "battery.charge": "90", "battery.charge.low": "20"}
eng.calib_check(eng.vars)
check("已中止", eng.calib is None)
check("原因是 FSD", any("收到 FSD" in m for m in msgs(2)), str(msgs(2)))

print("8. 采到 12 分钟放电样本 → 写进实测容量")
ok, _ = eng.calib_start(15)
started = eng.calib["started"]
t0 = started + 60                      # 标定开始 1 分钟后才拔电
c0, c1, mins = 100, 81, 12
run = [{"t": t0 + i * 60, "c": round(c0 + (c1 - c0) * i / float(mins), 2),
        "l": 11, "r": 900, "s": "OB"} for i in range(mins + 1)]
run.append({"t": t0 + mins * 60 + 5, "c": c1, "l": 11, "r": 900, "s": "OL"})
eng.trend.samples = run[:]
eng.vars = ob(charge=c1)
st = eng.calib_status()
check("识别到可用样本", st["sample_ready"] is True, str(st.get("sample")))
res = eng.calib_finish("单元测试")
check("有结果", res["ok"] is True, res["message"])
check("外推 50 分钟左右", 49 <= res["sample"]["minutes"] <= 52, str(res["sample"]))
check("事件里写了实测值", any("实测满电" in m for m in msgs(2)), str(msgs(2)))

print("8b. 建议放电时长跟着续航走（小电池别放 12 分钟）")
eng.vars = {"ups.status": "OL", "battery.runtime": "1200"}
check("续航 20 分钟 → 8 分钟", server.calib_hint(eng.vars) == 8, str(server.calib_hint(eng.vars)))
eng.vars = {"ups.status": "OL", "battery.runtime": "3600"}
check("续航 60 分钟 → 12 分钟", server.calib_hint(eng.vars) == 12, str(server.calib_hint(eng.vars)))
eng.vars = {"ups.status": "OL"}
check("读不到续航 → 10 分钟", server.calib_hint(eng.vars) == 10, str(server.calib_hint(eng.vars)))

print("9. 进程重启：标定状态作废，阈值不受影响")
server._save_calib({"started": int(time.time()), "until": int(time.time()) + 600,
                    "window": 15, "orig_onbatt": 300, "target": 1200, "hint": 12})
eng2 = server.Engine()
check("重启后没有残留标定", eng2.calib is None)
check("提示文件已清掉", not os.path.isfile(server.CALIB_PATH))
check("阈值仍是 300", eng2.effective_onbatt() == (300, False))
check("留下了中断说明", any("进程重启中断" in e.get("msg", "") for e in eng2.events.recent(3)))

print("\n全部 10 组通过")
