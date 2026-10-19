"""生成带样例数据的界面预览页（不连后端）。用法：
    python tools/gen_preview.py            # 默认展示「电池健康正常」
    python tools/gen_preview.py due        # 展示「电池需要更换」+ 顶部提示条
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.join(ROOT, "app", "public", "index.html")
out_path = os.path.join(ROOT, "preview", "demo.html")

mode = (sys.argv[1] if len(sys.argv) > 1 else "ok").lower()

BATTERY_OK = """{level:"ok", message:"已使用 4 天，距建议更换还有 727 天。",
    installed:"2026-10-15", age_days:4, life_months:24, replace_on:"2028-10-15", days_left:727,
    measured_minutes:42, baseline_minutes:42, decay_pct:0, samples:1, last_outage_at:NOW-5400,
    charge_low:20, test_date:"2026/10/12", test_age_days:7, charge_stuck_days:0}"""

BATTERY_DUE = """{level:"due", message:"实测容量比基线下降 38%（42 → 26 分钟），电池已明显衰减，建议更换。",
    installed:"2024-06-10", age_days:861, life_months:24, replace_on:"2026-06-10", days_left:-131,
    measured_minutes:26, baseline_minutes:42, decay_pct:38.1, samples:4, last_outage_at:NOW-86400,
    charge_low:20, test_date:"2026/03/02", test_age_days:231, charge_stuck_days:0}"""

battery = BATTERY_DUE if mode == "due" else BATTERY_OK

DEMO = """
<script>
/* 演示用：拦截 fetch 喂样例数据，不连后端。模式：%(mode)s */
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
var DEMO_STATE = {
  version:"1.2.0", connected:true, error:"", last_poll:NOW-2, now:NOW,
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
  battery: %(battery)s,
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
    {ts:NOW-8040, kind:"start", msg:"UPSLink 1.2.0 启动，监听 0.0.0.0:8099"}
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
var DEMO_CHECK = {ok:true, version:"1.2.0", checks:[
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
""" % {"battery": battery, "mode": mode}

src = open(src_path, encoding="utf-8").read()
marker = "<script>\nvar state = null;"
assert marker in src, "index.html 结构变了，找不到注入点"
src = src.replace(marker, DEMO + marker, 1)
src = src.replace("<title>UPSLink · UPS 联动</title>", "<title>UPSLink · 界面预览（样例数据）</title>")
src = src.replace("setTimeout(drawTrend, 300);",
                  "setTimeout(function(){ drawTrend(); "
                  "var b=document.getElementById('chartBox').getBoundingClientRect(); "
                  "onHover({clientX: b.left + b.width*0.55, clientY: b.top + 40}); }, 900);")

os.makedirs(os.path.dirname(out_path), exist_ok=True)
open(out_path, "w", encoding="utf-8").write(src)
print("written %s (%s 模式, %d bytes)" % (out_path, mode, len(src)))
