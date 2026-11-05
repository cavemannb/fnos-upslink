#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bark 推送的单元测试：不联网、不碰真机。

覆盖：
  1. bark_send 的参数校验与本地 HTTP 服务的收发（成功 / 返回码异常 / 网络失败）；
  2. evaluate() 的状态转换推送：断电开始、低电量去重、市电恢复、标记落盘；
  3. 进程重启不重推同一次断电；容量标定窗口内主动拔电不推送；开关关闭不推送。
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import server  # noqa: E402

TMP = tempfile.mkdtemp(prefix="upslink-bark-")
server.DATA_DIR = TMP
server.CONF_PATH = os.path.join(TMP, "config.json")
server.EVENT_PATH = os.path.join(TMP, "events.log")
server.BATTERY_PATH = os.path.join(TMP, "battery.json")
server.CALIB_PATH = os.path.join(TMP, "calibration.json")
server.CALIB_LOG_PATH = os.path.join(TMP, "calib-windows.json")
server.MAINT_PATH = os.path.join(TMP, "maintenance.json")
server.BARK_STATE_PATH = os.path.join(TMP, "bark_state.json")

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  OK   %s%s" % (label, (" · " + str(detail)) if detail != "" else ""))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (label, (" · " + str(detail)) if detail != "" else ""))


def make_engine(**conf_over):
    e = server.Engine()
    e.conf = dict(server.DEFAULT_CONF)
    e.conf.update(conf_over)
    e.bark_state = {"loss_pushed": False, "lb_pushed": False, "fsd_pushed": False}
    return e


def sim(e, status, charge="85.0"):
    e.vars = {"ups.status": status, "battery.charge": charge}
    e.evaluate()


# ---------------------------------------------------------------- 1. bark_send
print("1. bark_send 参数校验")
ok, msg = server.bark_send("https://api.day.app", "", "t", "b")
check("未填 Key 失败", not ok and "Key" in msg, msg)
ok, msg = server.bark_send("api.day.app", "KEY", "t", "b")
check("缺协议头失败", not ok and "http" in msg, msg)

print("\n2. bark_send 走本地 HTTP 服务")


class FakeBark(BaseHTTPRequestHandler):
    code = 200
    last = None

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        FakeBark.last = json.loads(self.rfile.read(ln) or b"{}")
        body = json.dumps({"code": FakeBark.code}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeBark)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % httpd.server_address[1]

ok, msg = server.bark_send(base, "KEY123", "标题", "内容")
check("code=200 成功", ok, msg)
check("device_key 正确", FakeBark.last.get("device_key") == "KEY123", FakeBark.last)
check("标题/正文正确", FakeBark.last.get("title") == "标题"
      and FakeBark.last.get("body") == "内容", FakeBark.last)
check("group 归类 UPSLink", FakeBark.last.get("group") == "UPSLink", FakeBark.last)

FakeBark.code = 400
ok, msg = server.bark_send(base, "KEY123", "t", "b")
check("code=400 判失败", not ok and "400" in msg, msg)

ok, msg = server.bark_send("http://127.0.0.1:1", "KEY", "t", "b", timeout=1)
check("连不上判失败", not ok, msg)
httpd.shutdown()

# ---------------------------------------------------------------- 3. 转换推送
print("\n3. 状态转换推送：断电开始")
calls = []
e = make_engine(bark_enabled=True, bark_key="k")
e.bark_push = lambda t, b: calls.append((t, b))
sim(e, "OL", "100.0")
sim(e, "OB", "85.0")
check("推了一条断电", len(calls) == 1 and "断电" in calls[0][0], calls)
check("正文带电量", "85%" in calls[0][1], calls[0][1])
check("标记落盘 loss_pushed", server._load_bark_state()["loss_pushed"])
sim(e, "OB", "84.0")
check("继续 OB 不重推", len(calls) == 1, len(calls))

print("\n4. 低电量只推一次")
sim(e, "OB LB", "20.0")
lb = [c for c in calls if "电量低" in c[0]]
check("LB 推一条", len(lb) == 1, lb)
sim(e, "OB LB", "19.0")
lb = [c for c in calls if "电量低" in c[0]]
check("第二个 LB 轮询不重推", len(lb) == 1, lb)

print("\n5. 恢复市电推送并清标记")
sim(e, "OL", "100.0")
rec = [c for c in calls if "恢复" in c[0]]
check("推了一条恢复", len(rec) == 1, rec)
check("标记已清空", e.bark_state == {"loss_pushed": False, "lb_pushed": False, "fsd_pushed": False})
sim(e, "OB", "80.0")
loss = [c for c in calls if "断电" in c[0]]
check("下一轮断电重新推", len(loss) == 2, len(loss))

print("\n6. 进程重启不重推同一次断电")
e2 = server.Engine()   # bark_state 从落盘文件恢复（此刻 loss_pushed=True）
e2.conf = dict(e.conf)
e2.bark_push = lambda t, b: calls.append((t, b))
sim(e2, "OB", "70.0")
loss2 = [c for c in calls if "断电" in c[0]]
check("重启后没有重复断电推送", len(loss2) == 2, len(loss2))

print("\n7. 标定窗口内主动拔电不推")
real_in_calib = server.in_calib_window
server.in_calib_window = lambda ts, windows=None: True
e3 = make_engine(bark_enabled=True, bark_key="k")
e3.bark_push = lambda t, b: calls.append((t, b))
sim(e3, "OL", "100.0")
sim(e3, "OB", "90.0")
loss3 = [c for c in calls if "断电" in c[0]]
check("标定中不推断电", len(loss3) == 2, len(loss3))
server.in_calib_window = real_in_calib

print("\n8. 开关关闭不推")
calls = []
e4 = make_engine(bark_enabled=False, bark_key="k")
e4.bark_push = lambda t, b: calls.append((t, b))
sim(e4, "OL", "100.0")
sim(e4, "OB LB", "30.0")
n = len(calls)
check("未启用时零推送", n == 0, calls)

print("\n9. FSD 推送一次")
e5 = make_engine(bark_enabled=True, bark_key="k")
e5.bark_push = lambda t, b: calls.append((t, b))
sim(e5, "OB FSD", "60.0")
fsd = [c for c in calls if "FSD" in c[0] or "强制" in c[0]]
check("FSD 推一条", len(fsd) == 1, fsd)
sim(e5, "OB FSD", "59.0")
fsd = [c for c in calls if "FSD" in c[0] or "强制" in c[0]]
check("FSD 不重推", len(fsd) == 1, fsd)

print("\n%d 项通过，%d 项失败。" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
