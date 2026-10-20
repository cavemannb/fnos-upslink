"""生成带样例数据的界面预览页（不连后端）。用法：
    python tools/gen_preview.py            # 默认展示「电池健康正常」
    python tools/gen_preview.py due        # 展示「电池需要更换」+ 顶部提示条
    python tools/gen_preview.py calib      # 展示「容量标定进行中」
    python tools/gen_preview.py maint      # 展示「该做电池保养了」
    python tools/gen_preview.py live       # 展示「拔电后自动进入放电计时」
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.join(ROOT, "app", "public", "index.html")
out_path = os.path.join(ROOT, "preview", "demo.html")

# 版本号从 manifest 读，避免每发一版就改这里
APP_VER = "0.0.0"
try:
    with open(os.path.join(ROOT, "manifest"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("version"):
                APP_VER = line.split("=")[1].strip()
                break
except OSError:
    pass

mode = (sys.argv[1] if len(sys.argv) > 1 else "ok").lower()

BATTERY_OK = """{level:"ok", message:"已使用 4 天，距建议更换还有 727 天。",
    installed:"2026-10-15", age_days:4, life_months:24, replace_on:"2028-10-15", days_left:727,
    measured_minutes:42, baseline_minutes:42, decay_pct:0, samples:1, last_outage_at:NOW-5400,
    charge_low:20, test_date:"2026/10/12", test_age_days:7, charge_stuck_days:0}"""

BATTERY_DUE = """{level:"due", message:"实测容量比基线下降 38%（42 → 26 分钟），电池已明显衰减，建议更换。",
    installed:"2024-06-10", age_days:861, life_months:24, replace_on:"2026-06-10", days_left:-131,
    measured_minutes:26, baseline_minutes:42, decay_pct:38.1, samples:4, last_outage_at:NOW-86400,
    charge_low:20, test_date:"2026/03/02", test_age_days:231, charge_stuck_days:0}"""

MAINT_OK = """{level:"ok", message:"短测还有 12 天，标定还有 141 天。",
    next_in_days:12,
    short:{interval:30, last_at:NOW-1555200, last_days:18, due_in_days:12, count:3},
    calib:{interval:180, last_at:NOW-3369600, last_days:39, due_in_days:141},
    deep_count:0, deep_last_days:null,
    recent:[{at:NOW-1555200, days:18, sec:104, drop:1.0, manual:false},
            {at:NOW-2160000, days:25, sec:96, drop:1.0, manual:false},
            {at:NOW-2505600, days:29, sec:88, drop:1.0, manual:false}]}"""

MAINT_DUE = """{level:"due", message:"短测已过计划日期 17 天（建议每 30 天一次）。拔掉市电 90 秒再插回，不用放空。",
    next_in_days:-17,
    short:{interval:30, last_at:NOW-4060800, last_days:47, due_in_days:-17, count:2},
    calib:{interval:180, last_at:NOW-3369600, last_days:39, due_in_days:141},
    deep_count:1, deep_last_days:23,
    recent:[{at:NOW-4060800, days:47, sec:162, drop:2.0, manual:false},
            {at:NOW-6739200, days:78, sec:95, drop:1.0, manual:false}]}"""

battery = BATTERY_DUE if mode == "due" else BATTERY_OK
maint = MAINT_DUE if mode in ("maint", "due") else MAINT_OK

DEMO = """<script>
var VARS = {
  "ups.status":"OL CHRG", "battery.charge":"100", "battery.charge.low":"20",
  "battery.runtime":"1200", "battery.type":"PbAc", "ups.load":"11",
  "ups.power.nominal":"600", "output.voltage":"230", "input.transfer.low":"184",
  "input.transfer.high":"264", "ups.beeper.status":"enabled", "ups.firmware":"02.1.I",
  "ups.serial":"SN-DEMO-0001", "ups.mfr":"EATON", "device.model":"SANTAK TG-BOX 600",
  "driver.name":"usbhid-ups", "driver.version":"2.8.0", "ups.delay.shutdown":"30",
  "ups.test.result":"Done and passed", "ups.test.date":"2026/10/12",
  "output.voltage.nominal":"230"
};
var NOW = Math.floor(Date.now()/1000);
var IDLE_CALIB = {active:false, last:null};
var CALIB_ON = {active:true, left:480, window:15, hint:12, phase:"discharging",
  threshold:1200, orig_threshold:300, charge:88, floor:50, sample_ready:false,
  sample:null, started:NOW-420};
var DEMO_STATE = {
  version:"%(ver)s", connected:true, error:"", last_poll:NOW-2, now:NOW,
  vars:VARS,
  metrics:{charge:100, charge_low:20, runtime:1200, load:11, power_nominal:600,
    power_va:66, power_rest:534, bat_type:"PbAc", bat_voltage:null, input_voltage:null,
    output_voltage:230, transfer_low:184, transfer_high:264, beeper:"enabled",
    mfr:"EATON", model:"SANTAK TG-BOX 600", serial:"SN-DEMO-0001", firmware:"02.1.I",
    driver:"usbhid-ups", delay:30, test_result:"Done and passed", test_date:"2026/10/12"},
  status:{raw:"OL CHRG", online:true, onbatt:false, low:false, fsd:false},
  status_text:"OL（市电在线） CHRG（充电中）",
  onbatt_elapsed:0, online_for:8040, shutdown_in:null,
  shutting_down:false, shutdown_reason:"", can_shutdown:true, has_password:false,
  battery: %(battery)s, maintenance: %(maint)s, calib_hint:8, calib_grace_left:0,
  last_outage:{at:NOW-1123200, seconds:270, ongoing:false},
  shutdown_threshold:300, calibration:IDLE_CALIB,
  config:{host:"192.168.1.20", port:3493, ups:"ups", username:"monuser",
    interval:5, onbatt_seconds:300, shutdown_on_lb:true, shutdown_on_fsd:true,
    auto_shutdown:true, battery_installed:"2026-10-15", battery_life_months:24,
    battery_warn_days:60},
  events:[
    {ts:NOW-120, kind:"test", msg:"测试连接成功，耗时 2 ms"},
    {ts:NOW-5400, kind:"online", msg:"市电恢复，停电计时清零"},
    {ts:NOW-5600, kind:"onbatt", msg:"开始使用电池供电"},
    {ts:NOW-5601, kind:"status", msg:"状态变化：OL（市电在线） → OB（电池供电） DISCHRG（放电中）"},
    {ts:NOW-86400, kind:"battery", msg:"已记录电池基线：满电到 20%% 约 42 分钟（当前负载）"},
    {ts:NOW-8040, kind:"start", msg:"UPSLink %(ver)s 启动，监听 0.0.0.0:8099"}
  ]
};

var OUTAGES = [[NOW-86400,1500],[NOW-5600,200]];
function genTrend(seconds){
  var step=300, start=NOW-seconds, pts=[];
  for (var t=start; t<=NOW; t+=step){
    var charge=100, ob=0, drop=0;
    for (var i=0;i<OUTAGES.length;i++){
      var a=OUTAGES[i][0], b=a+OUTAGES[i][1];
      if (t>=a && t<=b){ ob=1; drop=Math.round((t-a)/OUTAGES[i][1]*(OUTAGES[i][1]>1000?8:2)); }
    }
    charge = 100-drop;
    var load = 10 + Math.round(2.2*Math.sin(t/5400)+1.4*Math.cos(t/1700)) + (ob?2:0);
    pts.push([t, charge, Math.max(4,load), ob]);
  }
  return pts;
}
function fakeStats(sec){
  var total=0, longest=0, last=null, n=0;
  OUTAGES.forEach(function(o){
    if (o[0] < NOW-sec) return;
    n++; total+=o[1]; longest=Math.max(longest,o[1]); last=o[0]+o[1];
  });
  return {count:n, total_seconds:total, last_at:last, longest_seconds:longest,
          ongoing:false, window_start:NOW-sec};
}
var DEMO_CHECK = {ok:true, version:"%(ver)s", checks:[
  {level:"ok", title:"上游地址已配置", detail:"192.168.1.20:3493 · UPS 名称 ups"},
  {level:"ok", title:"数据目录可写", detail:"/vol1/@appdata/upslink"},
  {level:"ok", title:"配置文件存在", detail:"config.json"},
  {level:"ok", title:"上游 NUT 服务可达", detail:"最近轮询 13:58:02"},
  {level:"ok", title:"能读到 UPS 状态", detail:"OL CHRG · SANTAK TG-BOX 600"},
  {level:"ok", title:"具备关机权限", detail:"断电时能真正关闭飞牛。"},
  {level:"ok", title:"自动关机已启用", detail:"断电 300 秒后关机；LB / FSD 兜底已开。"},
  {level:"ok", title:"电池周期正常", detail:"已使用 4 天，距建议更换还有 727 天。"}
]};

window.fetch = function(url){
  var u = String(url), body;
  if (u.indexOf("/api/trend") >= 0){
    var secs = u.indexOf("7d") >= 0 ? 604800 : u.indexOf("6h") >= 0 ? 21600
             : u.indexOf("1h") >= 0 ? 3600 : 86400;
    body = {range: secs===604800?"7d":secs===3600?"1h":secs===21600?"6h":"24h",
            points: genTrend(secs), now: NOW, seconds: secs, sample_gap:300,
            keep_days:30, stats: fakeStats(secs)};
  } else if (u.indexOf("/api/selfcheck") >= 0){
    body = DEMO_CHECK;
  } else if (u.indexOf("/api/repair") >= 0){
    body = {ok:true, can_shutdown:true, message:"关机权限已恢复"};
  } else if (u.indexOf("/api/state") >= 0){
    body = DEMO_STATE;
  } else {
    body = {ok:true, ms:2, status_text:"OL（市电在线） CHRG（充电中）", vars:VARS,
            steps:["场景：OB（电池供电）· 模拟市电中断","断电持续 5 分钟 → 达到 300 秒阈值，将触发关机","本次为演练，未执行任何关机命令。"]};
  }
  return Promise.resolve({ok:true, status:200, json:function(){ return Promise.resolve(body); }});
};
</script>
""" % {"battery": battery, "maint": maint, "mode": mode, "ver": APP_VER}


def onbatt(demo, elapsed, shutdown_in, charge=None):
    """把样例数据改成「正在放电」。"""
    demo = demo.replace('status:{raw:"OL CHRG", online:true, onbatt:false, low:false, fsd:false}',
                        'status:{raw:"OB DISCHRG", online:false, onbatt:true, low:false, fsd:false}')
    demo = demo.replace('status_text:"OL（市电在线） CHRG（充电中）"',
                        'status_text:"OB（电池供电） DISCHRG（放电中）"')
    demo = demo.replace('"ups.status":"OL CHRG"', '"ups.status":"OB DISCHRG"')
    demo = demo.replace('onbatt_elapsed:0, online_for:8040',
                        'onbatt_elapsed:%d, online_for:0' % elapsed)
    demo = demo.replace('shutdown_in:null', 'shutdown_in:%d' % shutdown_in)
    if charge is not None:
        demo = demo.replace('"battery.charge":"100"', '"battery.charge":"%d"' % charge)
        demo = demo.replace('metrics:{charge:100', 'metrics:{charge:%d' % charge)
    return demo


if mode == "calib":
    DEMO = onbatt(DEMO, 420, 780, charge=91)
    DEMO = DEMO.replace('shutdown_threshold:300', 'shutdown_threshold:1200, calibrating:true')
    DEMO = DEMO.replace('calibration:IDLE_CALIB', 'calibration:CALIB_ON')
    DEMO = DEMO.replace('{ts:NOW-120, kind:"test", msg:"测试连接成功，耗时 2 ms"},',
                        '{ts:NOW-420, kind:"calibrate", msg:"开始容量标定：15 分钟窗口内不因断电计时关机（临时阈值 1200 秒）"},'
                        '{ts:NOW-120, kind:"test", msg:"测试连接成功，耗时 2 ms"},')
elif mode == "live":
    # 只是拔掉市电 64 秒：不点任何按钮，卡片自己进入「放电中 · 短测」
    DEMO = onbatt(DEMO, 64, 236, charge=99)

src = open(src_path, encoding="utf-8").read()
marker = "<script>\nvar state = null;"
assert marker in src, "index.html 结构变了，找不到注入点"
src = src.replace(marker, DEMO + marker, 1)

if mode in ("maint", "live"):
    # 预览页是静态样例：面板只能由这个示例按钮打开，真实界面绝不会自动弹窗
    bar = ('<div style="position:fixed;right:14px;bottom:14px;z-index:50;display:flex;'
           'align-items:center;gap:6px;padding:7px 9px;border-radius:11px;'
           'background:var(--panel);border:1px solid var(--line);'
           'box-shadow:0 6px 20px rgba(0,0,0,.10);font:12px/1 -apple-system,'
           'BlinkMacSystemFont,Segoe UI,sans-serif;color:var(--muted)">'
           '<span style="padding:0 2px">样例</span>'
           '<button onclick="openMaintSheet()" style="padding:5px 9px;border-radius:7px;'
           'border:1px solid var(--line);background:transparent;color:var(--text);'
           'font:inherit;cursor:pointer">保养面板</button>'
           '<button onclick="calibSheet()" style="padding:5px 9px;border-radius:7px;'
           'border:1px solid var(--line);background:transparent;color:var(--text);'
           'font:inherit;cursor:pointer">容量标定</button></div>')
    src = src.replace("</body>", bar + "</body>")

src = src.replace("<title>UPSLink · UPS 联动</title>",
                  "<title>UPSLink · 界面预览（样例数据）</title>")

# 悬停到曲线中段，让光标读数与提示框直接可见
src = src.replace("setTimeout(drawTrend, 300);",
                  "setTimeout(function(){ drawTrend(); "
                  "var b=document.getElementById('chartBox').getBoundingClientRect(); "
                  "onHover({clientX: b.left + b.width*0.55, clientY: b.top + 40}); }, 900);")

os.makedirs(os.path.dirname(out_path), exist_ok=True)
open(out_path, "w", encoding="utf-8").write(src)
print("written %s (%s 模式, %d bytes)" % (out_path, mode, len(src)))

# 追加深色验证副本（demo_dark.html）：把 dark media 查询固定为 .forceDark class，
# 便于在浅色系统上直接预览深色主题。每次生成都随 demo.html 重建，不会过时。
import re
dark = re.sub(r'@media \(prefers-color-scheme: dark\)\{\s*:root\{([^\0]*?)\}\s*\}',
              lambda m: '.forceDark{' + m.group(1) + '}', src)
dark = dark.replace('<html lang="zh-CN">', '<html lang="zh-CN" class="forceDark">')
dark_path = os.path.join(os.path.dirname(out_path), "demo_dark.html")
open(dark_path, "w", encoding="utf-8").write(dark)
