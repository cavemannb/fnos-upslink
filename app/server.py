#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UPSLink - 飞牛 fnOS 的 NUT 客户端（连接群晖/绿联/任何 NUT Server）

设计原则（照抄「电援联动」的安全边界）：
  1. 只有明确收到 OB / LB / FSD 才可能关机，网络异常一律不关机；
  2. 自动关机默认关闭，必须手动开启；
  3. OL DISCHRG 视为市电在线，不会因为出现 DISCHRG 就误判停电；
  4. 提供不会真实关机的「安全演练」。

只依赖 Python 3 标准库。
"""

import calendar
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_VERSION = "1.8.3"
PORT = int(os.environ.get("UPSLINK_PORT") or os.environ.get("TRIM_SERVICE_PORT") or "8099")
DATA_DIR = os.environ.get("TRIM_PKGVAR") or os.environ.get("UPSLINK_DATA") or "/opt/upslink/var"
CONF_PATH = os.path.join(DATA_DIR, "config.json")
BATTERY_PATH = os.path.join(DATA_DIR, "battery.json")
EVENT_PATH = os.path.join(DATA_DIR, "events.log")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
CMD_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir, "cmd"))
REPAIR_SCRIPT = os.path.join(CMD_DIR, "repair_shutdown.sh")

# 趋势采样：常规每 5 分钟一个点，状态一变立刻补点（前后各一个，保住阶梯）
TREND_KEEP_DAYS = 30
TREND_SAMPLE_GAP = 300
TREND_MAX_POINTS = 600
TREND_RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

# 容量标定：临时抬高「断电计时关机」阈值，换一次足够长的放电窗口
CALIB_PATH = os.path.join(DATA_DIR, "calibration.json")
CALIB_DEFAULT_MIN = 15          # 默认窗口（分钟）：够放 12 分钟，留 3 分钟余量
CALIB_MIN_MIN, CALIB_MAX_MIN = 6, 30
CALIB_MARGIN = 300              # 窗口之外的额外保护时间（秒）
CALIB_FLOOR_CHARGE = 50         # 电量降到这个数就中止，别把电池放空
CALIB_MIN_WINDOW = 8            # 与 measure_capacity 的门槛一致，用于提示文案
CALIB_HINT_MAX = 12             # 建议放电时长上限，别把电池放太狠
CALIB_GRACE = 180               # 标定结束后留给用户插回市电的宽限（秒）

# 电池保养：铅酸电池没有记忆效应，深度放电只会加速硫化。
# 所以这里排的是「短时轻放」和「半年一次标定」，不是「定期放空」。
MAINT_PATH = os.path.join(DATA_DIR, "maintenance.json")
MAINT_SHORT_DAYS = 30           # 短测建议间隔
MAINT_CALIB_DAYS = 180          # 标定建议间隔（6 个月）
MAINT_SHORT_MIN_SEC = 30        # 短于这个时长的状态抖动不算一次放电
MAINT_DEEP_CHARGE = 40          # 记录到的最低电量低于这个值 → 记一次深度放电
MAINT_KEEP_RECORDS = 12


# NUT 的 ups.status 是空格分隔的简写标志位，界面上统一补中文注释
STATUS_ZH = {
    "OL": "市电在线",
    "OB": "电池供电",
    "LB": "低电量",
    "HB": "电量低",
    "RB": "需更换电池",
    "CHRG": "充电中",
    "DISCHRG": "放电中",
    "FSD": "上游强制关机",
    "ALARM": "告警",
    "OFF": "UPS 输出关闭",
    "BYPASS": "旁路供电",
    "BOOST": "升压补偿",
    "TRIM": "降压补偿",
    "OVER": "超载",
    "CAL": "校准中",
    "WAIT": "等待",
    "ECO": "经济模式",
    "NOCOMM": "通信中断",
}

SCENARIO_ZH = {
    "ob": "OB（电池供电）· 模拟市电中断",
    "lb": "LB（低电量）",
    "fsd": "FSD（上游强制关机）",
    "offline": "离线（上游不可达）· 模拟断网",
}


def status_zh(raw):
    """把 'OL DISCHRG' 翻成 'OL（市电在线） DISCHRG（放电中）'"""
    parts = [p for p in str(raw or "").split() if p]
    if not parts:
        return "未知"
    return " ".join(p + "（" + STATUS_ZH[p] + "）" if p in STATUS_ZH else p for p in parts)


def can_shutdown():
    """有没有关机权限：root 直接算，否则看安装回调写下的标记（最小 sudo 规则是否生效）"""
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return True
    except Exception:
        pass
    try:
        with open(os.path.join(DATA_DIR, "shutdown_capable"), "r", encoding="utf-8") as f:
            return f.read().strip() == "1"
    except Exception:
        return False


def repair_shutdown():
    """重建关机权限。返回 (ok, message)。

    正常情况下：sudo 规则里已放行 cmd/repair_shutdown.sh，一次调用即可复原，
    无需重启进程（sudo 每次执行都会重新读 sudoers）。
    若规则本身也被清掉（大版本升级后可能发生），只能由 root 侧介入 ——
    应用中心的「停止 → 启动」会以 root 执行 cmd/main，启动时自动重建。
    """
    if not os.path.exists(REPAIR_SCRIPT):
        return False, "找不到修复脚本 %s，应用文件不完整，建议在应用中心重装。" % REPAIR_SCRIPT
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    cmd = [REPAIR_SCRIPT] if root else ["sudo", "-n", REPAIR_SCRIPT]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return False, ("系统里没有 sudo，无法自助修复；请在应用中心把「UPS 联动」"
                       "停止再启动一次，启动时以 root 重建权限。")
    except Exception as exc:
        return False, "调用修复脚本失败：%s" % exc
    if can_shutdown():
        return True, "关机权限已恢复，断电时本应用可以关闭飞牛。"
    if p.returncode != 0:
        return False, ("修复被拒绝（%s）。请到应用中心把「UPS 联动」停止再启动一次——"
                       "生命周期脚本以 root 运行，会在启动时自动重建权限，不必卸载重装。"
                       % (out[:160] or "sudo 未授权"))
    return False, "修复脚本已执行，但仍未拿到关机权限" + ("：%s" % out[:160] if out else "。")


DEFAULT_CONF = {
    "host": "",
    "port": 3493,
    "ups": "ups",
    "username": "",
    "password": "",
    "interval": 5,
    "onbatt_seconds": 300,
    "auto_shutdown": False,
    "shutdown_on_lb": True,
    "shutdown_on_fsd": True,
    "battery_installed": "",
    "battery_life_months": 24,
    "battery_warn_days": 60,
}

STATUS_KEYS = [
    "ups.status", "battery.charge", "battery.runtime", "ups.load",
    "device.model", "ups.mfr", "input.voltage", "battery.voltage",
]


# ---------------------------------------------------------------- 配置读写
def load_conf():
    conf = dict(DEFAULT_CONF)
    try:
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            conf.update(json.load(f) or {})
    except Exception:
        pass
    return conf


def save_conf(conf):
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF_PATH)


# ---------------------------------------------------------------- NUT 客户端
def nut_query(host, port, ups, username="", password="", timeout=4.0):
    """走 NUT 明文协议读 LIST VAR，只读，不需要授权即可取状态。"""
    sock = socket.create_connection((host, int(port)), timeout)
    sock.settimeout(timeout)
    fp = sock.makefile("rwb")

    def send(line):
        fp.write((line + "\n").encode("utf-8"))
        fp.flush()

    try:
        if username:
            send("USERNAME " + username)
            send("PASSWORD " + password)
        send("LIST VAR " + ups)
        out = {}
        while True:
            raw = fp.readline()
            if not raw:
                raise IOError("上游关闭了连接")
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line.startswith("END LIST VAR"):
                break
            if line.startswith("ERR "):
                raise IOError(line[4:].strip())
            m = re.match(r'^VAR\s+\S+\s+(\S+)\s+"(.*)"$', line)
            if m:
                out[m.group(1)] = m.group(2)
        if not out:
            raise IOError("上游返回空数据，检查 UPS 名称是否正确")
        return out
    finally:
        try:
            fp.close()
            sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 事件日志
class EventLog:
    def __init__(self, path, limit=200):
        self.path = path
        self.limit = limit
        self.items = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f.readlines()[-limit:]:
                    try:
                        self.items.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass

    def add(self, kind, message):
        item = {"ts": int(time.time()), "kind": kind, "msg": message}
        self.items.append(item)
        del self.items[:-self.limit]
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass
        try:
            sys.stderr.write("[%s] %s\n" % (kind, message))
            sys.stderr.flush()
        except Exception:
            pass

    def recent(self, n=50):
        return list(reversed(self.items[-n:]))


# ---------------------------------------------------------------- 趋势存储
class Trend:
    """电量/负载的时间序列，按天落盘（trend-YYYYMMDD.jsonl），保留 30 天。

    每个采样点是一行短键 JSON：{"t":时间戳,"c":电量,"l":负载,"r":续航,"s":"OL"}
    —— 短键是为了控制体积，30 天大约 700 KB。
    """

    def __init__(self, data_dir, keep_days=TREND_KEEP_DAYS, gap=TREND_SAMPLE_GAP):
        self.dir = data_dir
        self.keep = keep_days
        self.gap = gap
        self.lock = threading.Lock()
        self.samples = []
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            pass
        self._load()
        self._prune()

    # ---- 文件 ----
    def _path(self, day):
        return os.path.join(self.dir, "trend-%s.jsonl" % day)

    def _load(self):
        today = datetime.date.today()
        cutoff = time.time() - self.keep * 86400
        loaded = []
        for i in range(self.keep + 1, -1, -1):
            day = today - datetime.timedelta(days=i)
            path = self._path(day.strftime("%Y%m%d"))
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            s = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(s, dict) and s.get("t", 0) >= cutoff:
                            loaded.append(s)
            except Exception:
                continue
        loaded.sort(key=lambda s: s.get("t", 0))
        with self.lock:
            self.samples = loaded

    def _prune(self):
        """删掉超过保留期的按天文件；只在本进程启动时做一次，代价可忽略。"""
        cutoff = datetime.date.today() - datetime.timedelta(days=self.keep + 1)
        try:
            for name in os.listdir(self.dir):
                m = re.match(r"^trend-(\d{8})\.jsonl$", name)
                if not m:
                    continue
                try:
                    day = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        os.remove(os.path.join(self.dir, name))
                    except Exception:
                        pass
        except Exception:
            pass

    # ---- 写入 ----
    def add(self, ts, charge, load, runtime, status):
        status = (status or "").strip()
        with self.lock:
            last = self.samples[-1] if self.samples else None
            if last and status == last.get("s") and ts - last.get("t", 0) < self.gap:
                return False
            if last and status != last.get("s") and ts - last.get("t", 0) > 60:
                # 补一个「变化前一瞬」的点，画出来才是台阶而不是斜坡
                self.samples.append({"t": ts - 1, "c": last.get("c"), "l": last.get("l"),
                                     "r": last.get("r"), "s": last.get("s")})
            item = {"t": int(ts), "c": charge, "l": load, "r": runtime, "s": status}
            self.samples.append(item)
            cutoff = time.time() - self.keep * 86400
            if len(self.samples) > 20000:
                self.samples = [s for s in self.samples if s.get("t", 0) >= cutoff]
        try:
            day = datetime.date.today().strftime("%Y%m%d")
            with open(self._path(day), "a", encoding="utf-8") as f:
                f.write(json.dumps(item, separators=(",", ":")) + "\n")
        except Exception:
            pass
        return True

    # ---- 查询 ----
    def series(self, seconds, max_points=TREND_MAX_POINTS):
        now = int(time.time())
        cutoff = now - seconds
        with self.lock:
            pts = [s for s in self.samples if s.get("t", 0) >= cutoff]
        if not pts:
            return [], now
        # 桶平均降采样；桶内只要出现过 OB 就标成断电，避免断电区间被平均掉
        if len(pts) > max_points:
            n = len(pts)
            out = []
            for b in range(max_points):
                a = b * n // max_points
                c = (b + 1) * n // max_points
                if c > a:
                    out.append(self._avg(pts[a:c]))
            pts = out
        return pts, now

    @staticmethod
    def _avg(bucket):
        def mean(key):
            vals = [b[key] for b in bucket if isinstance(b.get(key), (int, float))]
            return round(sum(vals) / float(len(vals))) if vals else None
        ob = 1 if any("OB" in str(b.get("s") or "").split() for b in bucket) else 0
        return {"t": bucket[-1].get("t"), "c": mean("c"), "l": mean("l"),
                "r": mean("r"), "s": ("OB" if ob else (bucket[-1].get("s") or "")), "ob": ob}

    def report(self, seconds):
        pts, now = self.series(seconds)
        # 统计用原始采样算，不用降采样后的点 —— 否则 OB 标记会被扩散到整个桶，
        # 断电时长会被高估到桶宽（7 天视图下能差出十几分钟）。
        with self.lock:
            raw = [s for s in self.samples if s.get("t", 0) >= now - seconds]
        stats = self.stats(raw, now)
        return {
            "points": [[p["t"], p.get("c"), p.get("l"), p.get("ob", 1 if "OB" in str(p.get("s") or "").split() else 0)]
                       for p in pts],
            "now": now,
            "seconds": seconds,
            "sample_gap": self.gap,
            "keep_days": self.keep,
            "stats": stats,
        }

    def stats(self, samples, now):
        """断电统计：由原始采样序列反推，重启不丢，也不额外占存储。"""
        intervals = []
        start = None
        for s in samples:
            ob = "OB" in str(s.get("s") or "").split()
            if ob and start is None:
                start = s.get("t")
            elif not ob and start is not None:
                intervals.append((start, s.get("t")))
                start = None
        if start is not None:
            intervals.append((start, now))
        total = sum(max(0, (b or 0) - (a or 0)) for a, b in intervals)
        return {
            "count": len(intervals),
            "total_seconds": int(total),
            "last_at": intervals[-1][1] if intervals else None,
            "longest_seconds": int(max([max(0, (b or 0) - (a or 0)) for a, b in intervals] or [0])),
            "ongoing": bool(start is not None),
            "window_start": now - (samples[0].get("t") if samples else now),
        }


# ---------------------------------------------------------------- 指标派生
def _num(value):
    """NUT 的变量都是字符串，还可能是 NA；统一转成数字，拿不到就返回 None。"""
    if value is None:
        return None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else round(f, 1)


def build_metrics(vars_, st):
    """把 UPS 能给的原始变量整理成界面直接可用的指标。
    注意：TG-BOX 600 这类机型 usbhid-ups 不报 input.voltage / input.frequency /
    ups.temperature，这些字段会是 None，界面据此隐藏而不是显示成空值。"""
    load = _num(vars_.get("ups.load"))
    nominal = _num(vars_.get("ups.power.nominal"))
    va = round(load * nominal / 100.0) if (load is not None and nominal) else None
    return {
        "charge": _num(vars_.get("battery.charge")),
        "charge_low": _num(vars_.get("battery.charge.low")),
        "runtime": _num(vars_.get("battery.runtime")),
        "load": load,
        "power_nominal": nominal,
        "power_va": va,
        "power_rest": (nominal - va) if (va is not None and nominal) else None,
        "bat_type": vars_.get("battery.type"),
        "bat_voltage": _num(vars_.get("battery.voltage")),
        "input_voltage": _num(vars_.get("input.voltage")),
        "output_voltage": _num(vars_.get("output.voltage")),
        "transfer_low": _num(vars_.get("input.transfer.low")),
        "transfer_high": _num(vars_.get("input.transfer.high")),
        "beeper": vars_.get("ups.beeper.status"),
        "mfr": vars_.get("ups.mfr"),
        "model": vars_.get("device.model") or vars_.get("ups.model"),
        "serial": vars_.get("ups.serial"),
        "firmware": vars_.get("ups.firmware"),
        "driver": vars_.get("driver.name") or vars_.get("driver.version"),
        "delay": _num(vars_.get("ups.delay.shutdown")),
        "test_result": vars_.get("ups.test.result"),
        "test_date": vars_.get("ups.test.date"),
        "online": bool(st.get("online")),
    }


# ---------------------------------------------------------------- 电池健康
def _parse_date(text):
    """接受 2026-10-15 / 2026/10/15 / 20261015 三种写法。"""
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("/", "-").replace(".", "-")
    m = re.match(r"^(\d{4})-?(\d{1,2})-?(\d{1,2})$", s)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _add_months(d, months):
    y = d.year + (d.month - 1 + int(months)) // 12
    m = (d.month - 1 + int(months)) % 12 + 1
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _load_battery_state():
    try:
        with open(BATTERY_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_battery_state(state):
    try:
        tmp = BATTERY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BATTERY_PATH)
    except Exception:
        pass


def ob_runs(trend):
    """把趋势采样切成一段段电池供电区间，供容量计算和保养统计共用。

    open=True 表示这段还在放电（最后一条采样仍是 OB），此时「时长」只是
    到目前为止的跨度，调用方要自己决定是否采信。
    """
    try:
        samples = list(trend.samples)
    except Exception:
        return []
    runs, cur = [], []
    for s in samples:
        ob = "OB" in str(s.get("s") or "").upper().split()
        if ob:
            cur.append(s)
        elif cur:
            runs.append((cur, False))
            cur = []
    if cur:
        runs.append((cur, True))

    out = []
    for run, is_open in runs:
        a, b = run[0], run[-1]
        cs = [x.get("c") for x in run if isinstance(x.get("c"), (int, float))]
        loads = [x.get("l") for x in run if x.get("l")]
        out.append({
            "start": a.get("t", 0),
            "at": b.get("t", 0),
            "seconds": max(0, int(b.get("t", 0) - a.get("t", 0))),
            "c0": a.get("c"),
            "c1": b.get("c"),
            "min_c": min(cs) if cs else None,
            "load": (sum(loads) / float(len(loads))) if loads else None,
            "n": len(run),
            "open": is_open,
        })
    return out


def measure_capacity(trend, charge_low=20):
    """从历史断电区间反推电池可用时长，比固件报的 runtime 更贴合真实老化。

    只用「时长够、电量真的掉了」的区间——短于 8 分钟或掉电不足 3% 的样本
    噪声太大，直接丢弃。返回值按当前负载归一化由调用方完成。
    """
    low = charge_low if charge_low is not None else 20
    out = []
    for r in ob_runs(trend):
        if r["n"] < 2:
            continue
        minutes = r["seconds"] / 60.0
        c0, c1 = r["c0"], r["c1"]
        if minutes < 8 or c0 is None or c1 is None:
            continue
        drop = c0 - c1
        if drop < 3 or c0 <= low:
            continue
        full = (c0 - low) * minutes / drop           # 当时从 c0 掉到 low 需要多久
        out.append({
            "at": r["at"],
            "minutes": round(full, 1),
            "window_min": round(minutes, 1),
            "drop": round(drop, 1),
            "load": round(r["load"], 1) if r["load"] else None,
        })
    return out[-5:]


def capacity_runs_since(trend, charge_low, since_ts):
    """标定窗口内新出现的可用样本（含正在放电、还没来电的那一段）。"""
    try:
        runs = measure_capacity(trend, charge_low)
    except Exception:
        return []
    return [r for r in runs if r.get("at", 0) >= since_ts]


def _load_calib():
    try:
        with open(CALIB_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or None
    except Exception:
        return None


def _save_calib(state):
    try:
        tmp = CALIB_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CALIB_PATH)
    except Exception:
        pass


def _clear_calib():
    try:
        if os.path.isfile(CALIB_PATH):
            os.remove(CALIB_PATH)
    except Exception:
        pass


def battery_health(conf, vars_, trend, events=None):
    """电池健康：投运月龄 + 实测容量衰减 + 充电异常 + UPS 自检记录。

    设计原则：拿不到足够证据就给 unknown，不猜、不吓人。
    """
    state = _load_battery_state()
    today = datetime.date.today()
    life = int(conf.get("battery_life_months") or 24)
    warn_days = int(conf.get("battery_warn_days") or 60)

    installed = _parse_date(conf.get("battery_installed"))
    age_days = (today - installed).days if installed else None
    replace_on = _add_months(installed, life) if installed else None
    days_left = (replace_on - today).days if replace_on else None

    if installed is None:
        level, message = "unknown", "未登记电池投运日期，无法判断使用时长。在下方配置里填一次更换日期即可。"
    elif days_left is not None and days_left <= 0:
        level, message = "due", "电池已超出 %d 个月建议周期（%s 投运），建议尽快更换。" % (life, installed)
    elif days_left is not None and days_left <= 30:
        level, message = "due", "电池将在 %d 天后到期，建议本月安排更换。" % days_left
    elif days_left is not None and days_left <= warn_days:
        level, message = "warn", "电池还有 %d 天到期，可以开始物色替换电池。" % days_left
    else:
        level, message = "ok", "已使用 %d 天，距建议更换还有 %d 天。" % (age_days, days_left)

    # ---- 实测容量（相对基线）----
    charge_low = _num(vars_.get("battery.charge.low")) or 20
    load_now = _num(vars_.get("ups.load"))
    runs = measure_capacity(trend, charge_low)
    measured = None
    if runs:
        vals = sorted(r["minutes"] for r in runs)
        median = vals[(len(vals) - 1) // 2]     # 取偏保守的一侧，宁可早提醒
        ref_load = runs[-1].get("load")
        if ref_load and load_now:
            measured = round(median * ref_load / float(load_now), 1)
        else:
            measured = round(median, 1)

    baseline = state.get("baseline_minutes")
    if measured and not baseline:
        state["baseline_minutes"] = measured
        state["baseline_at"] = int(time.time())
        baseline = measured
        if events:
            events.add("battery", "已记录电池基线：满电到 %d%% 约 %.0f 分钟（当前负载）" % (int(charge_low), measured))
    decay = None
    if measured and baseline:
        decay = round(max(0.0, (baseline - measured) / float(baseline)) * 100, 1)

    # 容量衰减比时间更可信：真的测出来掉了 30%，就不等日期了
    if decay is not None and decay >= 30 and level in ("ok", "warn", "unknown"):
        level = "due"
        message = "实测容量比基线下降 %.0f%%（%.0f → %.0f 分钟），电池已明显衰减，建议更换。" % (
            decay, baseline, measured)
    elif decay is not None and decay >= 15 and level == "ok":
        level = "warn"
        message = "实测容量较基线下降 %.0f%%，继续观察。" % decay

    # ---- 充电异常：连续多天充不满 ----
    charge = _num(vars_.get("battery.charge"))
    if charge is not None and charge >= 99:
        state["last_full_ts"] = int(time.time())
    last_full = state.get("last_full_ts")
    stuck_days = None
    if last_full:
        stuck_days = int((time.time() - last_full) / 86400)
    if stuck_days is not None and stuck_days >= 7 and level == "ok":
        level = "warn"
        message = "已连续 %d 天充不到 100%%，电池内阻可能上升，留意容量变化。" % stuck_days

    # ---- UPS 自检记录（ups.test.date）----
    test_date = _parse_date(vars_.get("ups.test.date"))
    test_age = (today - test_date).days if test_date else None

    state["level"] = level
    state["checked_at"] = int(time.time())
    _save_battery_state(state)

    return {
        "level": level,
        "message": message,
        "installed": installed.isoformat() if installed else None,
        "age_days": age_days,
        "life_months": life,
        "replace_on": replace_on.isoformat() if replace_on else None,
        "days_left": days_left,
        "measured_minutes": measured,
        "baseline_minutes": baseline,
        "decay_pct": decay,
        "samples": len(runs),
        "last_outage_at": runs[-1]["at"] if runs else None,
        "charge_low": charge_low,
        "test_date": vars_.get("ups.test.date"),
        "test_age_days": test_age,
        "charge_stuck_days": stuck_days,
    }


# ---------------------------------------------------------------- 电池保养
def _maint_default():
    return {
        "created_at": int(time.time()),
        "last_short_at": None,
        "last_calib_at": None,
        "scan_at": 0,          # 已经统计过的放电区间，避免同一段重复计数
        "brief": [],           # 短时放电记录
        "deep_count": 0,
        "deep_last_at": None,
    }


def _load_maint():
    state = _maint_default()
    try:
        with open(MAINT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if isinstance(data, dict):
            state.update(data)
    except Exception:
        pass
    return state


def _save_maint(state):
    try:
        tmp = MAINT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MAINT_PATH)
    except Exception:
        pass


def mark_calibrated(at=None):
    """标定或一次足够长的真实断电都算「刷新了容量样本」。"""
    state = _load_maint()
    at = int(at or time.time())
    state["last_calib_at"] = max(int(state.get("last_calib_at") or 0), at)
    _save_maint(state)


def maintenance_status(trend, events=None):
    """电池保养排期：短测（轻放）+ 标定（测容量），外加深度放电计数。

    只做建议，不碰任何关机逻辑——这里产出的 level 最多影响界面颜色。
    """
    now = int(time.time())
    state = _load_maint()
    dirty = False

    # ---- 1. 把「已经结束」的放电区间过一遍：短的记短测，长的记标定，过深的记警告
    scan = int(state.get("scan_at") or 0)
    for r in ob_runs(trend):
        if r["open"] or r["at"] <= scan:
            continue
        scan = r["at"]
        dirty = True
        sec = r["seconds"]
        if sec >= MAINT_SHORT_MIN_SEC:
            state["last_short_at"] = max(int(state.get("last_short_at") or 0), r["at"])
            brief = list(state.get("brief") or [])
            brief.append({"at": r["at"], "sec": sec, "c0": r["c0"], "c1": r["c1"]})
            state["brief"] = brief[-MAINT_KEEP_RECORDS:]
            if events and sec >= 60:
                events.add("maint", "电池短测：放电 %d 秒（电量 %s%% → %s%%），已计入保养记录"
                           % (sec, r["c0"] if r["c0"] is not None else "?",
                              r["c1"] if r["c1"] is not None else "?"))
        minutes = sec / 60.0
        drop = None
        if isinstance(r["c0"], (int, float)) and isinstance(r["c1"], (int, float)):
            drop = r["c0"] - r["c1"]
        if minutes >= CALIB_MIN_WINDOW and drop is not None and drop >= 3:
            state["last_calib_at"] = max(int(state.get("last_calib_at") or 0), r["at"])
            if events:
                events.add("maint", "容量样本已更新：这次放电 %.1f 分钟、电量降 %.0f%%，下一次标定建议半年后"
                           % (minutes, drop))
        if r["min_c"] is not None and r["min_c"] <= MAINT_DEEP_CHARGE:
            state["deep_count"] = int(state.get("deep_count") or 0) + 1
            state["deep_last_at"] = r["at"]
            if events:
                events.add("maint", "深度放电：记录到的最低电量只有 %d%%。铅酸电池放到这么低会加速硫化，"
                                    "下次尽量在 60%% 以上就恢复市电。" % r["min_c"])
    if dirty:
        state["scan_at"] = scan
        _save_maint(state)

    # ---- 2. 排期
    base = int(state.get("created_at") or now)
    last_short = int(state.get("last_short_at") or 0)
    last_calib = int(state.get("last_calib_at") or 0)
    short_due = (last_short or base) + MAINT_SHORT_DAYS * 86400
    calib_due = (last_calib or base) + MAINT_CALIB_DAYS * 86400
    # 四舍五入取整天数：floor 除法会把「刚好满 10 天 + 1 秒」算成 11 天，
    # 对用户的直觉是「还差一点才算 11 天」，所以不足半天不进位。
    short_days = int(round((short_due - now) / 86400.0))
    calib_days = int(round((calib_due - now) / 86400.0))
    nearest = min(short_days, calib_days)

    # 过期不做「推迟」：提醒就是排期本身，用户做没做都用放电记录说话。
    # 文案只报「已过计划日期 N 天」，不催、不静默。
    if nearest <= 0:
        level = "due"
        if calib_days <= short_days:
            over = int(-calib_days)
            when = ("已过计划日期 %d 天" % over) if over > 0 else "今天到计划日期"
            message = ("容量标定%s（建议每 %d 个月一次）。放电到 80%% 左右就够，不要放空。"
                       % (when, MAINT_CALIB_DAYS // 30))
        else:
            over = int(-short_days)
            when = ("已过计划日期 %d 天" % over) if over > 0 else "今天到计划日期"
            message = ("短测%s（建议每 %d 天一次）。拔掉市电 90 秒再插回，不用放空。"
                       % (when, MAINT_SHORT_DAYS))
    elif nearest <= 7:
        level = "warn"
        message = "快到保养时间了：短测还有 %d 天，标定还有 %d 天。" % (short_days, calib_days)
    else:
        level = "ok"
        if not last_short and not last_calib:
            message = ("还没有维护记录。建议每 %d 天做一次短测（拔市电 1~2 分钟），"
                       "每 %d 个月做一次标定。" % (MAINT_SHORT_DAYS, MAINT_CALIB_DAYS // 30))
        else:
            message = "短测还有 %d 天，标定还有 %d 天。" % (short_days, calib_days)

    return {
        "level": level,
        "message": message,
        "next_in_days": int(nearest),
        "short": {
            "interval": MAINT_SHORT_DAYS,
            "last_at": last_short or None,
            "last_days": (now - last_short) // 86400 if last_short else None,
            "due_in_days": int(short_days),
            "count": len(state.get("brief") or []),
        },
        "calib": {
            "interval": MAINT_CALIB_DAYS,
            "last_at": last_calib or None,
            "last_days": (now - last_calib) // 86400 if last_calib else None,
            "due_in_days": int(calib_days),
        },
        "deep_count": int(state.get("deep_count") or 0),
        "deep_last_days": (now - int(state["deep_last_at"])) // 86400
        if state.get("deep_last_at") else None,
        # 最近几次放电：全部由趋势采样自动统计，手工补记会带 manual=True。
        # 界面上直接列出来，用户就不用靠「记一次」按钮自己报数。
        "recent": _recent_briefs(state, now),
    }


def _recent_briefs(state, now, limit=4):
    out = []
    for b in list(state.get("brief") or [])[-limit:][::-1]:
        try:
            at = int(b.get("at") or 0)
        except (TypeError, ValueError):
            continue
        drop = None
        c0, c1 = b.get("c0"), b.get("c1")
        if isinstance(c0, (int, float)) and isinstance(c1, (int, float)):
            drop = round(c0 - c1, 1)
        out.append({
            "at": at,
            "days": (now - at) // 86400 if at else None,
            "sec": b.get("sec"),
            "drop": drop,
            "manual": bool(b.get("manual")),
        })
    return out


def calib_hint(vars_):
    """按当前固件续航推荐放电时长：目标掉 20~25%，但不低于测量门槛、不超过 12 分钟。

    小电池（固件报 20 分钟续航）放满 12 分钟会直接撞到 50% 保护线，
    所以建议时长必须跟着续航走，不能写死。
    """
    runtime = _num((vars_ or {}).get("battery.runtime"))
    hint = int(round(runtime / 60.0 * 0.25)) if runtime else 10
    return max(CALIB_MIN_WINDOW, min(CALIB_HINT_MAX, hint))


# ---------------------------------------------------------------- 规则引擎
class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.conf = load_conf()
        self.events = EventLog(EVENT_PATH)
        self.trend = Trend(DATA_DIR)
        self._outage_cache = None
        self.vars = {}
        self.connected = False
        self.last_error = ""
        self.last_poll = 0
        self.onbatt_since = None
        self.online_since = None
        self.prev_status = ""
        self.shutting_down = False
        self.shutdown_reason = ""
        self.battery = {}
        self.battery_at = 0
        self.maint = {}
        self.maint_at = 0
        self.calib = None
        self.calib_last = None
        self.calib_grace_until = 0
        # 标定状态只存在于内存 + 一个提示文件；配置本身从不改动，
        # 所以进程重启后直接作废是安全的一侧。
        if _load_calib():
            _clear_calib()
            self.events.add("calibrate", "上次容量标定因进程重启中断；断电阈值本就没被改动，已回原值")

    # ---- 状态解析 ----
    @staticmethod
    def parse(status):
        toks = (status or "").upper().replace(",", " ").split()
        return {
            "raw": (status or "").strip(),
            "online": "OL" in toks,
            "onbatt": "OB" in toks,
            "low": "LB" in toks,
            "fsd": "FSD" in toks,
            "stale": "DATA-STALE" in toks,
        }

    def snapshot(self):
        with self.lock:
            conf = dict(self.conf)
            vars_ = dict(self.vars)
            onbatt_since = self.onbatt_since
            online_since = self.online_since
            connected = self.connected
            err = self.last_error
            last_poll = self.last_poll
        st = self.parse(vars_.get("ups.status", ""))
        wait_eff, calibrating = self.effective_onbatt(conf)
        elapsed = int(time.time() - onbatt_since) if onbatt_since else 0
        remain = None
        if st["onbatt"] and wait_eff > 0:
            remain = max(0, wait_eff - elapsed)
        return {
            "version": APP_VERSION,
            "connected": connected,
            "error": err,
            "last_poll": last_poll,
            "now": int(time.time()),
            "vars": vars_,
            "metrics": build_metrics(vars_, st),
            "status": st,
            "status_text": status_zh(st["raw"]) if st["raw"] else "—",
            "onbatt_elapsed": elapsed,
            "online_for": int(time.time() - online_since) if (online_since and st["online"]) else 0,
            "shutdown_in": remain,
            "shutdown_threshold": wait_eff,
            "calibrating": calibrating,
            "shutting_down": self.shutting_down,
            "shutdown_reason": self.shutdown_reason,
            "can_shutdown": can_shutdown(),
            "battery": self.battery,
            "maintenance": self.maint,
            "last_outage": self._last_outage(),
            "calibration": self.calib_status(vars_),
            "calib_hint": calib_hint(vars_),
            "calib_grace_left": max(0, int(self.calib_grace_until - time.time()))
            if self.calib_grace_until else 0,
            "config": {k: v for k, v in conf.items() if k not in ("password",)},
            "has_password": bool(conf.get("password")),
            "events": self.events.recent(80),
            "trend": {"sample_gap": TREND_SAMPLE_GAP, "keep_days": TREND_KEEP_DAYS},
        }

    # ---- 有记录以来最近一次断电（不受趋势图时间跨度影响）----
    def _last_outage(self):
        with self.lock:
            n = len(self.trend.samples)
            last_t = self.trend.samples[-1].get("t", 0) if self.trend.samples else 0
            cached = self._outage_cache
            if cached and cached[0] == (n, last_t):
                return cached[1]
        info = None
        runs = ob_runs(self.trend)
        if runs:
            r = runs[-1]
            info = {"at": r["at"], "seconds": r["seconds"], "ongoing": bool(r["open"])}
        with self.lock:
            self._outage_cache = ((n, last_t), info)
        return info

    # ---- 关机 ----
    def do_shutdown(self, reason, dry_run=False):
        if self.shutting_down:
            return False
        if dry_run:
            self.events.add("drill", "演练：本应关机 → " + reason)
            return False
        self.shutting_down = True
        self.shutdown_reason = reason
        self.events.add("shutdown", "执行关机：" + reason)
        cmd = ["/sbin/shutdown", "-h", "+0"]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.events.add("error", "关机命令执行失败：%s" % exc)
            self.shutting_down = False
            return False
        return True

    # ---- 容量标定 ----
    def effective_onbatt(self, conf=None):
        """标定期间用「抬高后的阈值」，但配置文件一个字都不改。

        这样即使进程被杀、机器重启，也不存在「阈值被留在 20 分钟」的残留风险。

        标定结束（到点 / 撞到电量保护线 / 手动结束）时如果还在放电，
        再给 CALIB_GRACE 秒宽限：否则原来 5 分钟的阈值早已被超过，
        程序会在同一轮轮询里立刻关机——标定就变成了「测一下就关机」。
        """
        c = self.calib
        conf = conf if conf is not None else self.conf
        wait = int(conf.get("onbatt_seconds") or 0)
        now = time.time()
        if c and now < int(c.get("until") or 0):
            return max(wait, int(c.get("target") or 0)), True
        grace_until = int(self.calib_grace_until or 0)
        if grace_until > now and self.onbatt_since and wait > 0:
            elapsed = int(now - self.onbatt_since)
            left = int(grace_until - now)
            return elapsed + left, False
        return wait, False

    def calib_start(self, minutes=CALIB_DEFAULT_MIN):
        with self.lock:
            if self.calib:
                return False, "标定已在进行中"
            conf = dict(self.conf)
            if not conf.get("host"):
                return False, "尚未配置上游地址，先连上 UPS 再标定"
            try:
                window = int(minutes)
            except (TypeError, ValueError):
                window = CALIB_DEFAULT_MIN
            window = max(CALIB_MIN_MIN, min(CALIB_MAX_MIN, window))
            orig = int(conf.get("onbatt_seconds") or 0)
            now = int(time.time())
            hint = min(window - 2, calib_hint(self.vars))
            self.calib = {
                "started": now,
                "until": now + window * 60,
                "window": window,
                "orig_onbatt": orig,
                "target": max(orig, window * 60 + CALIB_MARGIN),
                "hint": hint,
            }
            calib = dict(self.calib)
        self.calib_grace_until = 0
        _save_calib(calib)
        self.events.add("calibrate", "开始容量标定：%d 分钟窗口内不因断电计时关机（临时阈值 %d 秒）"
                        % (calib["window"], calib["target"]))
        self.battery_at = 0
        return True, "已进入标定模式，请拔掉 UPS 市电，保持放电约 %d 分钟" % calib["hint"]

    def calib_status(self, vars_=None):
        with self.lock:
            c = dict(self.calib) if self.calib else None
            last = dict(self.calib_last) if self.calib_last else None
        if not c:
            return {"active": False, "last": last}
        v = vars_ if vars_ is not None else self.vars
        st = self.parse(v.get("ups.status", ""))
        low = _num(v.get("battery.charge.low")) or 20
        runs = capacity_runs_since(self.trend, low, c["started"])
        return {
            "active": True,
            "left": max(0, int(c["until"]) - int(time.time())),
            "window": c["window"],
            "hint": c.get("hint"),
            "started": c["started"],
            "phase": "discharging" if st["onbatt"] else "waiting",
            "threshold": c["target"],
            "orig_threshold": c["orig_onbatt"],
            "charge": _num(v.get("battery.charge")),
            "floor": CALIB_FLOOR_CHARGE,
            "sample_ready": bool(runs),
            "sample": runs[-1] if runs else None,
        }

    def calib_finish(self, reason, abort=False):
        with self.lock:
            c = self.calib
            if not c:
                return None
            self.calib = None
        _clear_calib()
        self.battery_at = 0                 # 下次轮询立刻重算电池健康
        low = _num(self.vars.get("battery.charge.low")) or 20
        runs = capacity_runs_since(self.trend, low, c["started"])
        if runs:
            r = runs[-1]
            msg = "标定结束（%s）：实测满电到 %d%% 约 %.0f 分钟（放电 %.1f 分钟、电量降 %.0f%%）" % (
                reason, int(low), r["minutes"], r.get("window_min") or 0, r.get("drop") or 0)
        elif abort:
            msg = "标定中止（%s）：本次未采到可用样本" % reason
        else:
            msg = "标定结束（%s）：未采到可用样本——放电不足 %d 分钟或电量降幅不足 3%%" % (
                reason, CALIB_MIN_WINDOW)
        self.events.add("calibrate", msg)
        result = {"ok": bool(runs), "message": msg, "at": int(time.time()),
                  "sample": runs[-1] if runs else None}
        with self.lock:
            self.calib_last = result
        # 结束瞬间还在放电 → 给一段宽限时间，别把「测一下」变成「立刻关机」
        st = self.parse(self.vars.get("ups.status", ""))
        if st["onbatt"]:
            self.calib_grace_until = int(time.time()) + CALIB_GRACE
            self.events.add("calibrate", "仍在电池供电：%d 秒内插回市电即可，超时按原策略关机"
                            % CALIB_GRACE)
            result["grace"] = CALIB_GRACE
            result["message"] = msg + "。现在还在放电，已宽限 %d 秒，请尽快插回市电" % CALIB_GRACE
        else:
            self.calib_grace_until = 0
        if runs:
            mark_calibrated(result["at"])      # 标定成功即刷新保养排期
        self.maint_at = 0
        self.wake.set()
        return result

    def calib_check(self, vars_):
        """每个轮询周期检查：到点结束、低电量/强制关机立即中止。"""
        c = self.calib
        if not c:
            return
        if time.time() >= int(c.get("until") or 0):
            self.calib_finish("窗口结束")
            return
        st = self.parse(vars_.get("ups.status", ""))
        if st["fsd"]:
            self.calib_finish("收到 FSD", abort=True)
        elif st["low"]:
            self.calib_finish("UPS 报告低电量", abort=True)
        else:
            charge = _num(vars_.get("battery.charge"))
            if charge is not None and charge <= CALIB_FLOOR_CHARGE:
                self.calib_finish("电量降到 %d%% 保护线" % charge, abort=True)

    # ---- 电池保养 ----
    def refresh_maintenance(self, force=False):
        """60 秒算一次：要遍历 30 天的放电区间，没必要每个快照都重算。"""
        if not force and time.time() - self.maint_at < 60:
            return self.maint
        try:
            m = maintenance_status(self.trend, self.events)
        except Exception:
            return self.maint
        with self.lock:
            self.maint = m
        self.maint_at = time.time()
        return m

    # ---- 单次轮询 ----
    def poll_once(self):
        with self.lock:
            conf = dict(self.conf)
        self.refresh_maintenance()
        if not conf.get("host"):
            with self.lock:
                self.connected = False
                self.last_error = "尚未配置上游地址"
            return
        try:
            vars_ = nut_query(
                conf["host"], conf["port"], conf["ups"],
                conf.get("username", ""), conf.get("password", ""),
            )
            with self.lock:
                self.vars = vars_
                self.connected = True
                self.last_error = ""
                self.last_poll = int(time.time())
            # 趋势：5 分钟一点，状态变化时立即补点（Trend.add 内部判断）
            try:
                self.trend.add(self.last_poll,
                               _num(vars_.get("battery.charge")),
                               _num(vars_.get("ups.load")),
                               _num(vars_.get("battery.runtime")),
                               vars_.get("ups.status", ""))
            except Exception:
                pass
            # 电池健康：60 秒算一次就够了（要遍历 30 天样本）
            if time.time() - self.battery_at > 60:
                try:
                    b = battery_health(conf, vars_, self.trend, self.events)
                    with self.lock:
                        self.battery = b
                    self.battery_at = time.time()
                except Exception:
                    pass
        except Exception as exc:
            with self.lock:
                was = self.connected
                self.connected = False
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
            if was:
                self.events.add("offline", "与上游断开，不执行任何关机动作：" + str(exc))
            return
        self.calib_check(vars_)
        self.evaluate()

    # ---- 规则判断 ----
    def evaluate(self, drill=False):
        with self.lock:
            conf = dict(self.conf)
            status = self.vars.get("ups.status", "")
            onbatt_since = self.onbatt_since
        st = self.parse(status)
        now = time.time()

        if st["raw"] != self.prev_status:
            prev = self.parse(self.prev_status)
            if st["online"] and not prev["online"]:
                with self.lock:
                    self.online_since = now
            self.events.add("status", "状态变化：%s → %s" % (
                status_zh(self.prev_status) if self.prev_status else "(空)",
                status_zh(st["raw"])))
            self.prev_status = st["raw"]

        if st["online"]:
            if onbatt_since:
                self.events.add("online", "市电恢复，停电计时清零")
            with self.lock:
                self.onbatt_since = None
                self.calib_grace_until = 0
            return

        if st["onbatt"]:
            if onbatt_since is None:
                with self.lock:
                    self.onbatt_since = now
                self.events.add("onbatt", "开始使用电池供电")
                onbatt_since = now
            elapsed = now - onbatt_since
            if st["low"] and conf["shutdown_on_lb"] and conf["auto_shutdown"]:
                self.do_shutdown("UPS 报告低电量（LB）", dry_run=drill)
                return
            if st["fsd"] and conf["shutdown_on_fsd"] and conf["auto_shutdown"]:
                self.do_shutdown("上游要求强制关机（FSD）", dry_run=drill)
                return
            wait, _ = self.effective_onbatt(conf)
            if wait > 0 and elapsed >= wait and conf["auto_shutdown"]:
                reason = "断电已持续 %d 秒" % wait
                grace = int(self.calib_grace_until or 0)
                if grace and now >= grace:
                    reason = "标定结束后 %d 秒内未恢复市电" % CALIB_GRACE
                    self.calib_grace_until = 0
                self.do_shutdown(reason, dry_run=drill)

    # ---- 后台线程 ----
    def loop(self):
        while True:
            try:
                self.poll_once()
            except Exception:
                self.events.add("error", traceback.format_exc(limit=2))
            with self.lock:
                interval = max(2, int(self.conf.get("interval") or 5))
            self.wake.wait(interval)
            self.wake.clear()


# ---------------------------------------------------------------- HTTP 层
ENGINE = Engine()


def is_private(ip):
    if ip in ("127.0.0.1", "::1"):
        return True
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$", ip or "")
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254)


def selfcheck():
    """系统升级后用来自检的一把体检。全部在应用内完成，不需要 SSH。
    返回 level 为 ok / warn / err 的条目列表。"""
    checks = []

    def add(level, title, detail=""):
        checks.append({"level": level, "title": title, "detail": detail})

    conf = dict(ENGINE.conf)

    # 1) 配置是否还在（系统升级后最容易被重置的就是这里）
    if conf.get("host"):
        add("ok", "上游地址已配置", "%s:%s · UPS 名称 %s" % (conf["host"], conf["port"], conf["ups"]))
    else:
        add("err", "上游地址为空", "配置被重置了。重新填群晖 IP、端口 3493、UPS 名称 ups 后保存。")

    # 2) 数据目录可写（升级后挂载点或权限变了就会失败）
    try:
        probe = os.path.join(DATA_DIR, ".selfcheck")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        add("ok", "数据目录可写", DATA_DIR)
    except Exception as exc:
        add("err", "数据目录不可写", "%s：%s" % (DATA_DIR, exc))

    # 3) 配置文件存在
    if os.path.isfile(CONF_PATH):
        add("ok", "配置文件存在", CONF_PATH)
    else:
        add("warn", "配置文件不存在", "将使用默认值，保存一次配置即可生成。")

    # 4) 上游连通性（看最近一次轮询）
    with ENGINE.lock:
        connected = ENGINE.connected
        last_poll = ENGINE.last_poll
        err = ENGINE.last_error
        st = dict(ENGINE.vars)
    if connected:
        add("ok", "上游 NUT 服务可达", "最近轮询 %s" % time.strftime("%H:%M:%S", time.localtime(last_poll or time.time())))
    else:
        add("err", "连不上上游 NUT 服务", err or "检查群晖是否开启网络 UPS 服务器、允许列表是否含飞牛 IP、3493 端口是否放行。")

    # 5) 读到的数据是否完整
    if st.get("ups.status"):
        add("ok", "能读到 UPS 状态", "%s · %s" % (st.get("ups.status"), st.get("device.model") or "未知型号"))
    else:
        add("warn", "读不到 UPS 状态变量", "上游可能拒绝该用户读取（群晖检查 upsd.users 的权限）。")

    # 6) 关机权限（系统大版本升级后 /etc/sudoers.d 有可能被重写）
    if can_shutdown():
        add("ok", "具备关机权限", "断电时能真正关闭飞牛。")
    else:
        add("err", "没有关机权限",
            "点本页「修复关机权限」即可重建；若提示被拒绝，到应用中心把本应用停止再启动一次。无需 SSH。")

    # 7) 自动关机开关
    if conf.get("auto_shutdown"):
        add("ok", "自动关机已启用", "断电 %s 秒后关机；LB / FSD 兜底%s。" % (
            conf.get("onbatt_seconds"),
            "已开" if conf.get("shutdown_on_lb") and conf.get("shutdown_on_fsd") else "部分开启"))
    else:
        add("warn", "自动关机未启用", "目前只监控、不关机。确认演练结果无误后再勾选。")

    # 8) 电池更换周期 / 容量衰减
    b = dict(ENGINE.battery)
    if b.get("level") == "due":
        add("warn", "电池需要更换", b.get("message", ""))
    elif b.get("level") == "warn":
        add("warn", "电池接近更换周期", b.get("message", ""))
    elif b.get("level") == "ok":
        add("ok", "电池周期正常", b.get("message", ""))
    else:
        add("warn", "电池投运日期未登记",
            "在「关机策略与配置」里填一次电池更换日期，倒计时与容量基线才会开始工作。")

    return {"ok": all(c["level"] != "err" for c in checks), "version": APP_VERSION, "checks": checks}


class Handler(BaseHTTPRequestHandler):
    server_version = "upslink/" + APP_VERSION

    def log_message(self, fmt, *args):
        return

    # ---- 工具 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(PUBLIC_DIR, rel))
        if not full.startswith(PUBLIC_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            return self._json(ENGINE.snapshot())
        if path == "/api/health":
            return self._json({"ok": True, "version": APP_VERSION})
        if path == "/api/selfcheck":
            return self._json(selfcheck())
        if path == "/api/trend":
            rng = (parse_qs(parsed.query).get("range") or ["24h"])[0]
            seconds = TREND_RANGES.get(rng, 86400)
            data = ENGINE.trend.report(seconds)
            data["range"] = rng
            return self._json(data)
        return self._static(path)

    def do_POST(self):
        if not is_private(self.client_address[0]):
            return self._json({"error": "仅允许局域网访问"}, 403)
        path = self.path.split("?")[0]
        body = self._body()

        if path == "/api/config":
            with ENGINE.lock:
                conf = dict(ENGINE.conf)
                for key in ("host", "ups", "username", "password"):
                    if key in body:
                        conf[key] = str(body[key]).strip()
                if body.get("clear_password"):
                    conf["password"] = ""
                for key in ("port", "interval", "onbatt_seconds"):
                    if key in body:
                        try:
                            conf[key] = int(body[key])
                        except (TypeError, ValueError):
                            pass
                for key in ("auto_shutdown", "shutdown_on_lb", "shutdown_on_fsd"):
                    if key in body:
                        conf[key] = bool(body[key])
                if "battery_installed" in body:
                    raw = str(body.get("battery_installed") or "").strip()
                    parsed = _parse_date(raw)
                    if raw and parsed is None:
                        return self._json({"error": "电池投运日期格式应为 2026-10-15"}, 400)
                    conf["battery_installed"] = parsed.isoformat() if parsed else ""
                for key in ("battery_life_months", "battery_warn_days"):
                    if key in body:
                        try:
                            conf[key] = int(body[key])
                        except (TypeError, ValueError):
                            pass
                conf["battery_life_months"] = max(6, min(120, int(conf["battery_life_months"])))
                conf["battery_warn_days"] = max(7, min(180, int(conf["battery_warn_days"])))
                conf["port"] = max(1, min(65535, int(conf["port"])))
                conf["interval"] = max(2, min(300, int(conf["interval"])))
                conf["onbatt_seconds"] = max(0, min(86400, int(conf["onbatt_seconds"])))
                if conf["auto_shutdown"] and not conf["host"]:
                    return self._json({"error": "未填写上游地址，不能开启自动关机"}, 400)
                if conf["auto_shutdown"] and conf["onbatt_seconds"] == 0 \
                        and not conf["shutdown_on_lb"] and not conf["shutdown_on_fsd"]:
                    return self._json({"error": "所有关机条件都为 0/关闭，等于不会关机"}, 400)
                ENGINE.conf = conf
                save_conf(conf)
                ENGINE.battery_at = 0          # 配置变了，下次轮询立即重算电池健康
            ENGINE.events.add("config", "配置已更新（自动关机：%s）" % ("开" if conf["auto_shutdown"] else "关"))
            ENGINE.wake.set()
            return self._json({"ok": True, "config": {k: v for k, v in conf.items() if k != "password"}})

        if path == "/api/repair":
            ok, message = repair_shutdown()
            ENGINE.events.add("repair" if ok else "error",
                              "关机权限修复：" + message)
            return self._json({"ok": ok, "message": message, "can_shutdown": can_shutdown()})

        if path == "/api/test":
            conf = ENGINE.conf
            if not conf.get("host"):
                return self._json({"ok": False, "error": "未填写上游地址"}, 400)
            t0 = time.time()
            try:
                vars_ = nut_query(conf["host"], conf["port"], conf["ups"],
                                  conf.get("username", ""), conf.get("password", ""))
                ms = int((time.time() - t0) * 1000)
                ENGINE.events.add("test", "测试连接成功，耗时 %d ms" % ms)
                return self._json({
                    "ok": True, "ms": ms,
                    "status_text": status_zh(vars_.get("ups.status", "")),
                    "vars": {k: vars_.get(k) for k in STATUS_KEYS if k in vars_},
                })
            except Exception as exc:
                ENGINE.events.add("test", "测试连接失败：%s" % exc)
                return self._json({"ok": False, "error": str(exc)})

        if path == "/api/calibrate":
            if body.get("action") == "stop":
                res = ENGINE.calib_finish("手动结束")
                return self._json({"ok": True, "result": res,
                                   "calibration": ENGINE.calib_status()})
            ok, msg = ENGINE.calib_start(body.get("minutes") or CALIB_DEFAULT_MIN)
            if not ok:
                return self._json({"ok": False, "error": msg}, 400)
            return self._json({"ok": True, "message": msg,
                               "calibration": ENGINE.calib_status()})

        if path == "/api/maint":
            action = str(body.get("action") or "scan")
            if action == "done":
                state = _load_maint()
                at = int(time.time())
                state["last_short_at"] = at
                brief = list(state.get("brief") or [])
                brief.append({"at": at, "sec": None, "manual": True})
                state["brief"] = brief[-MAINT_KEEP_RECORDS:]
                _save_maint(state)
                ENGINE.events.add("maint", "手动记录：已完成一次电池短测")
            ENGINE.maint_at = 0
            return self._json({"ok": True, "maintenance": ENGINE.refresh_maintenance(force=True)})

        if path == "/api/drill":
            scenario = body.get("scenario", "ob")
            minutes = max(0, min(1440, int(body.get("minutes") or 5)))
            conf = ENGINE.conf
            steps = ["场景：" + SCENARIO_ZH.get(scenario, scenario)]
            if not conf.get("auto_shutdown"):
                steps.append("当前「自动关机」是关闭的，任何场景都不会真的关机。")
            if scenario == "ob":
                wait, calibrating = ENGINE.effective_onbatt()
                steps.append("断电持续 %d 分钟 → %s" % (
                    minutes,
                    ("标定期间临时阈值 %d 秒，" % wait if calibrating else "")
                    + ("达到 %d 秒阈值，将触发关机" % wait if wait and minutes * 60 >= wait
                       else "未达到 %d 秒阈值，不关机" % wait if wait else "阈值为 0，只靠 LB/FSD 兜底")))
            elif scenario == "lb":
                steps.append("收到 LB（低电量）→ %s" % (
                    "启用，将立即关机" if conf.get("shutdown_on_lb") else "未启用，不会因此关机"))
            elif scenario == "fsd":
                steps.append("收到 FSD（上游强制关机）→ %s" % (
                    "启用，将立即关机" if conf.get("shutdown_on_fsd") else "未启用，不会因此关机"))
            elif scenario == "offline":
                steps.append("断网 / 上游离线 → 只记录日志，绝不关机（这是设计约束，不可改）")
            steps.append("本次为演练，未执行任何关机命令。")
            ENGINE.events.add("drill", "安全演练：" + SCENARIO_ZH.get(scenario, scenario))
            return self._json({"ok": True, "scenario": scenario, "steps": steps})

        return self._json({"error": "not found"}, 404)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONF_PATH):
        save_conf(dict(DEFAULT_CONF))
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        # 端口被占（通常是旧实例没退干净）。先写事件再退出，日志里能直接看懂。
        ENGINE.events.add("error", "端口 %d 被占用，启动失败：%s" % (PORT, exc))
        print("端口 %d 被占用，无法启动：%s" % (PORT, exc), flush=True)
        sys.exit(1)
    ENGINE.events.add("start", "UPSLink %s 启动，监听 0.0.0.0:%d" % (APP_VERSION, PORT))
    threading.Thread(target=ENGINE.loop, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
