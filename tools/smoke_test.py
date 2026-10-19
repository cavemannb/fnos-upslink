#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UPSLink 冒烟测试：本地起一个假 NUT Server + 真 server.py，走一遍核心链路。
   在飞牛上也可以跑：python3 tools/smoke_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

PORT = 18099
NUT_PORT = 13493

# 假 NUT 上报的状态，测试里可以改（模拟断电 / 恢复）
FAKE_STATUS = ["OL CHRG"]


def fake_nut():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", NUT_PORT))
    srv.listen(5)

    def handle(conn):
        fp = conn.makefile("rwb")
        try:
            while True:
                line = fp.readline()
                if not line:
                    break
                cmd = line.decode("utf-8", "replace").strip()
                if cmd.startswith("LIST VAR"):
                    fp.write((
                        "BEGIN LIST VAR ups\n"
                        'VAR ups ups.status "%s"\n'
                        'VAR ups battery.charge "100.0"\n'
                        'VAR ups battery.charge.low "20"\n'
                        'VAR ups battery.runtime "3600"\n'
                        'VAR ups battery.type "PbAc"\n'
                        'VAR ups ups.load "23"\n'
                        'VAR ups ups.power.nominal "600"\n'
                        'VAR ups output.voltage "230.0"\n'
                        'VAR ups input.transfer.low "170"\n'
                        'VAR ups input.transfer.high "280"\n'
                        'VAR ups ups.beeper.status "enabled"\n'
                        'VAR ups ups.firmware "02.1.I"\n'
                        'VAR ups ups.serial "5B1234"\n'
                        'VAR ups ups.mfr "EATON"\n'
                        'VAR ups device.model "Back-UPS BK650M2-CH"\n'
                        'VAR ups driver.name "usbhid-ups"\n'
                        'VAR ups input.voltage "220.0"\n'
                        "END LIST VAR ups\n" % FAKE_STATUS[0]
                    ).encode())
                    fp.flush()
                elif cmd.startswith("USERNAME") or cmd.startswith("PASSWORD"):
                    fp.write(b"OK\n")
                    fp.flush()
                else:
                    fp.write(b"ERR UNKNOWN-COMMAND\n")
                    fp.flush()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path), data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main():
    threading.Thread(target=fake_nut, daemon=True).start()
    data_dir = tempfile.mkdtemp(prefix="upslink-test-")
    env = dict(os.environ, UPSLINK_PORT=str(PORT), UPSLINK_DATA=data_dir)
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "server.py")
    proc = subprocess.Popen([sys.executable, app], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        assert req("GET", "/api/health")["ok"]
        print("1. health            OK")

        j = req("POST", "/api/config", {"host": "127.0.0.1", "port": NUT_PORT,
                                        "ups": "ups", "interval": 2, "onbatt_seconds": 300})
        assert j.get("ok"), j
        print("2. 保存配置           OK")

        time.sleep(1.5)
        st = req("GET", "/api/state")
        assert st["connected"], st
        assert st["status"]["online"] and not st["status"]["onbatt"]
        assert st["vars"]["device.model"] == "Back-UPS BK650M2-CH"
        print("3. 读取状态           OK  OL / %s" % st["vars"]["device.model"])

        m = st["metrics"]
        assert m["charge"] == 100 and m["load"] == 23, m
        assert m["power_va"] == 138 and m["power_rest"] == 462, m
        assert m["charge_low"] == 20 and m["bat_type"] == "PbAc", m
        assert m["transfer_low"] == 170 and m["transfer_high"] == 280, m
        print("4. 指标派生           OK  负载 %s VA / %s VA，阈值 %s%%" % (
            m["power_va"], m["power_nominal"], m["charge_low"]))

        FAKE_STATUS[0] = "OB DISCHRG"
        time.sleep(3.5)
        st = req("GET", "/api/state")
        assert st["status"]["onbatt"] and not st["shutting_down"], st
        FAKE_STATUS[0] = "OL CHRG"
        time.sleep(3.5)
        tr = req("GET", "/api/trend?range=1h")
        assert tr["points"] and tr["keep_days"] == 30, tr
        assert any(p[3] == 1 for p in tr["points"]), tr
        assert tr["stats"]["count"] >= 1, tr
        print("5. 趋势采样           OK  %d 点，断电 %d 次（台阶与统计都正确）" % (
            len(tr["points"]), tr["stats"]["count"]))

        t = req("POST", "/api/test", {})
        assert t["ok"], t
        print("6. 测试连接           OK  %d ms" % t["ms"])

        sc = req("GET", "/api/selfcheck")
        assert sc["checks"], sc
        assert any(c["level"] == "ok" and "上游" in c["title"] for c in sc["checks"]), sc
        print("7. 健康自检           OK  %d 项：%s" % (
            len(sc["checks"]), " / ".join(c["level"] for c in sc["checks"])))

        d = req("POST", "/api/drill", {"scenario": "ob", "minutes": 10})
        assert d["ok"] and d["steps"], d
        assert "场景：" in d["steps"][0], d
        print("8. 安全演练           OK  %s" % " / ".join(d["steps"]))

        rp = req("POST", "/api/repair", {})
        assert "message" in rp and "can_shutdown" in rp, rp
        print("9. 修复关机权限       OK  ok=%s（%s）" % (
            rp["ok"], rp["message"][:60].replace("\n", " ")))

        req("POST", "/api/config", {"port": 13499})
        time.sleep(4)
        st = req("GET", "/api/state")
        assert not st["connected"] and not st["shutting_down"], st
        print("10. 断连保护          OK  上游离线不触发关机")

        print("\n全部通过。")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
