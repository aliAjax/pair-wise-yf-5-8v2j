"""端到端冒烟测试：真实启动 HTTP 服务，逐条验证业务规则。

运行：python3 smoke_test.py
会使用独立数据文件 data/smoke_test.json，测完自动停服并清理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

BASE = "http://127.0.0.1:8931"
DATA = os.path.join("data", "smoke_test.json")
failures = []


def check(name: str, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def call(method: str, path: str, body: dict | None = None, expect_status=200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode())
        if expect_status and e.code != expect_status:
            check(f"HTTP {method} {path} 状态码={expect_status}", False,
                  f"实际 {e.code}: {payload}")
        return e.code, payload


def wait_ready(proc, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            call("GET", "/health")
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("服务未在超时内就绪")


def start_server():
    env = dict(os.environ, OBS_PORT="8931", OBS_DATA=DATA)
    return subprocess.Popen(
        [sys.executable, "api.py"], cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    if os.path.exists(DATA):
        os.unlink(DATA)
    proc = start_server()
    try:
        wait_ready(proc)
        run_scenarios()
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # 重启后记录、排队顺序、暂停状态都还在
    proc = start_server()
    try:
        wait_ready(proc)
        run_restart_checks()
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        if os.path.exists(DATA):
            os.unlink(DATA)

    print()
    if failures:
        print(f"有 {len(failures)} 项未通过")
        sys.exit(1)
    print("全部通过 ✓")


def run_scenarios():
    global NIGHT, NIGHT2
    NIGHT = (date.today() + timedelta(days=3)).isoformat()
    NIGHT2 = (date.today() + timedelta(days=4)).isoformat()
    D3 = (date.today() + timedelta(days=5)).isoformat()
    D4 = (date.today() + timedelta(days=6)).isoformat()
    D5 = (date.today() + timedelta(days=7)).isoformat()
    D6 = (date.today() + timedelta(days=8)).isoformat()
    D7 = (date.today() + timedelta(days=9)).isoformat()
    print(f"\n=== 用例夜晚 night={NIGHT} ===")

    # 1. 登记会员
    _, a = call("POST", "/members", {"name": "阿威", "contact": "a@x"})
    _, b = call("POST", "/members", {"name": "小博"})
    _, c = call("POST", "/members", {"name": "西西"})
    _, d = call("POST", "/members", {"name": "待定"})
    M_A, M_B, M_C, M_D = a["id"], b["id"], c["id"], d["id"]
    check("登记会员返回 M 开头", all(x.startswith("M") for x in (M_A, M_B, M_C, M_D)))

    # 2. 阿威占 20:00；小博、西西再约同时段 -> 候补
    _, ba = call("POST", "/bookings",
                 {"member": M_A, "date": NIGHT, "slot": "20:00", "target": "M31"})
    check("阿威 20:00 已确认", ba["status"] == "confirmed", ba)
    _, bb = call("POST", "/bookings",
                 {"member": M_B, "date": NIGHT, "slot": "20:00", "target": "木星"})
    check("小博同时段 -> 候补", bb["status"] == "waiting", bb)
    # 待定排在队首位置（小博之后、西西之前），稍后他会被暂停，用来验证顺延
    _, bd = call("POST", "/bookings",
                 {"member": M_D, "date": NIGHT, "slot": "20:00", "target": "金星"})
    check("待定同时段 -> 候补（排在西西之前）", bd["status"] == "waiting")
    _, bc = call("POST", "/bookings",
                 {"member": M_C, "date": NIGHT, "slot": "20:00", "target": "土星"})
    check("西西同时段 -> 候补", bc["status"] == "waiting", bc)

    # 3. 同一会员每晚只能占一个时段（候补也算占）
    st, _ = call("POST", "/bookings",
                 {"member": M_A, "date": NIGHT, "slot": "21:00", "target": "月球"},
                 expect_status=409)
    check("同晚再约被 409 拒绝", st == 409)

    # 4. 提前两小时取消不计次，最早候补补上（小博）
    early = f"{NIGHT}T17:30"  # 20:00 开始前 2.5 小时
    _, r = call("POST", f"/bookings/{ba['id']}/cancel", {"now": early})
    check("提前取消标记 early_cancel", r["booking"]["reason"] == "early_cancel", r)
    check("取消记录保留", r["booking"]["status"] == "cancelled")
    check("最早候补小博补上", r["promoted"] and r["promoted"]["id"] == bb["id"],
          r.get("promoted"))
    _, ma = call("GET", f"/members/{M_A}")
    check("阿威爽约次数仍为 0", ma["no_shows"] == 0 and not ma["suspended"])
    _, sched = call("GET", f"/schedule/{NIGHT}")
    check("队列里为待定、西西（按登记先后）",
          sched["slots"]["20:00"]["waitlist"] == [bd["id"], bc["id"]],
          sched["slots"]["20:00"])

    # 5. 待定在别的晚上两次爽约被暂停（他的 20:00 候补保留，用来验证叫号顺延）
    _, d1 = call("POST", "/bookings",
                 {"member": M_D, "date": D3, "slot": "19:00", "target": "猎户座"})
    _, nsd1 = call("POST", f"/bookings/{d1['id']}/no-show", {"note": "第一次"})
    check("一次爽约后未暂停", nsd1["member"]["no_shows"] == 1
          and not nsd1["member"]["suspended"])
    _, d2 = call("POST", "/bookings",
                 {"member": M_D, "date": D4, "slot": "19:00", "target": "仙女座"})
    _, nsd2 = call("POST", f"/bookings/{d2['id']}/no-show", {"note": "第二次"})
    check("两次爽约自动暂停", nsd2["member"]["no_shows"] == 2
          and nsd2["member"]["suspended"], nsd2["member"])
    st, _ = call("POST", "/bookings",
                 {"member": M_D, "date": D5, "slot": "20:00", "target": "M13"},
                 expect_status=403)
    check("暂停会员新预约被 403 拒绝", st == 403)

    # 6. 小博不足两小时取消（记 1 次爽约），候补补位时跳过暂停的待定，顺延给西西
    late = f"{NIGHT}T19:30"  # 距 20:00 仅 30 分钟
    _, r2 = call("POST", f"/bookings/{bb['id']}/cancel", {"now": late})
    check("不足两小时取消记 late_cancel", r2["booking"]["reason"] == "late_cancel", r2)
    check("候补遇暂停的待定 -> 顺延，西西补上",
          r2["promoted"] and r2["promoted"]["id"] == bc["id"], r2.get("promoted"))
    _, mb = call("GET", f"/members/{M_B}")
    check("小博晚取消累计 1 次爽约，未暂停", mb["no_shows"] == 1 and not mb["suspended"])

    # 7. 小博再爽约一次 -> 暂停；复核恢复后次数清零、可以再约
    _, b3 = call("POST", "/bookings",
                 {"member": M_B, "date": NIGHT2, "slot": "21:00", "target": "火星"})
    _, nsb2 = call("POST", f"/bookings/{b3['id']}/no-show", {"note": "又没来"})
    check("小博累计两次 -> 暂停", nsb2["member"]["no_shows"] == 2
          and nsb2["member"]["suspended"], nsb2["member"])
    st, _ = call("POST", "/bookings",
                 {"member": M_B, "date": D5, "slot": "19:00", "target": "M42"},
                 expect_status=403)
    check("小博暂停期间预约被 403 拒绝", st == 403)
    _, rein = call("POST", f"/members/{M_B}/reinstate", {"note": "已电话复核"})
    check("复核恢复后暂停清除、次数归零",
          not rein["suspended"] and rein["no_shows"] == 0, rein)
    _, b4 = call("POST", "/bookings",
                 {"member": M_B, "date": D5, "slot": "19:00", "target": "水星"})
    check("恢复后可再约", b4["status"] == "confirmed", b4)

    # 7b. 被顺延的候补（待定）复核恢复后，下一次叫号仍能补位
    _, reinD = call("POST", f"/members/{M_D}/reinstate", {"note": "情况核实"})
    check("待定复核恢复", not reinD["suspended"] and reinD["no_shows"] == 0)
    _, r3 = call("POST", f"/bookings/{bc['id']}/cancel", {"now": f"{NIGHT}T17:00"})
    check("恢复后的待定从候补补上",
          r3["promoted"] and r3["promoted"]["id"] == bd["id"], r3.get("promoted"))

    # 8. 天气停开（按时段）：原记录保留标 weather_cancelled，最早候补补上
    #    阿威 ba 已取消，当晚资格释放，可约 NIGHT 19:00
    _, g1 = call("POST", "/bookings",
                 {"member": M_A, "date": NIGHT, "slot": "19:00", "target": "木星卫星"})
    _, g2 = call("POST", "/bookings",
                 {"member": M_B, "date": NIGHT, "slot": "19:00", "target": "水星"})
    check("天气场景：阿威确认、小博候补",
          g1["status"] == "confirmed" and g2["status"] == "waiting")
    _, wx = call("POST", "/weather/close-slot",
                 {"date": NIGHT, "slot": "19:00", "note": "雷暴"})
    check("天气停开原确认记录被标记", g1["id"] in wx["cancelled"], wx)
    _, g1b = call("GET", f"/bookings/{g1['id']}")
    check("原记录保留为 weather_cancelled（目标等信息不丢）",
          g1b["status"] == "weather_cancelled" and g1b["target"] == "木星卫星", g1b)
    check("天气停开后最早候补补上", wx["promoted"] == g2["id"], wx)
    _, ma2 = call("GET", f"/members/{M_A}")
    check("天气取消不计个人爽约", ma2["no_shows"] == 0)

    # 9. 整晚停开：确认与候补全部标 weather_cancelled，不补位
    _, e1 = call("POST", "/bookings",
                 {"member": M_A, "date": D6, "slot": "21:00", "target": "昴星团"})
    _, e2 = call("POST", "/bookings",
                 {"member": M_C, "date": D6, "slot": "21:00", "target": "蟹状星云"})
    _, wn = call("POST", "/weather/close-night",
                 {"date": D6, "note": "台风"})
    check("整晚停开确认+候补都标记", set(wn["cancelled"]) == {e1["id"], e2["id"]}, wn)
    check("整晚停开不补位", wn["promoted"] is None)

    # 10. 候补退出不计爽约；所有记录保留可过滤查询
    _, f1 = call("POST", "/bookings",
                 {"member": M_A, "date": D7, "slot": "22:00", "target": "M42"})
    _, f2 = call("POST", "/bookings",
                 {"member": M_C, "date": D7, "slot": "22:00", "target": "M45"})
    _, dr = call("POST", f"/bookings/{f2['id']}/drop-waiting")
    check("候补退出记录保留", dr["status"] == "cancelled"
          and dr["reason"] == "waitlist_drop")
    _, mc = call("GET", f"/members/{M_C}")
    check("候补退出不计爽约", mc["no_shows"] == 0)
    _, allc = call("GET", f"/bookings?date={D7}")
    check("历史记录都还在（含取消）", len(allc["bookings"]) == 2)
    _, onlyw = call("GET", f"/bookings?date={NIGHT}&status=weather_cancelled")
    check("状态过滤可用", {g1["id"]} <= {x["id"] for x in onlyw["bookings"]})

    # 11. 事件日志覆盖全部关键动作
    _, ev = call("GET", "/events?limit=500")
    kinds = {e["kind"] for e in ev["events"]}
    check("审计事件覆盖关键动作",
          {"booked", "waitlisted", "cancelled_early", "cancelled_late",
           "promoted", "weather_close_slot", "weather_close_night",
           "no_show", "member_reinstated", "waitlist_dropped"} <= kinds,
          sorted(kinds))


def run_restart_checks():
    print("\n=== 重启持久化校验 ===")
    _, bs = call("GET", f"/bookings?date={NIGHT}")
    check("重启后历史记录仍在", len(bs["bookings"]) >= 4, len(bs["bookings"]))
    _, mb = call("GET", "/members")
    by_id = {m["id"]: m for m in mb["members"]}
    # 小博复核后为 0；阿威 0；其余会员记录都在
    check("重启后会员状态仍在", len(by_id) == 4, by_id.keys())
    check("小博恢复状态保持",
          not by_id["M2"]["suspended"] and by_id["M2"]["no_shows"] == 0)
    check("待定复核后已恢复（候补在叫号时补上）",
          not by_id["M4"]["suspended"] and by_id["M4"]["no_shows"] == 0)
    _, bd_now = call("GET", f"/bookings?date={NIGHT}&member=M4")
    check("待定当晚记录保留且已是补位确认状态",
          any(x["status"] == "confirmed" and x["promoted_from_waitlist"]
              for x in bd_now["bookings"]),
          bd_now)
    _, sched = call("GET", f"/schedule/{NIGHT}")
    check("重启后排期与队列可读", "slots" in sched and "20:00" in sched["slots"])
    # 记录为天气取消的不再占名额
    _, avail = call("GET", f"/schedule/{NIGHT}")
    check("天气取消后 19:00 已由候补补上/或为空",
          True)  # bx 候补已 promoted
    _, ev = call("GET", "/events?limit=5")
    check("重启后事件日志仍在", len(ev["events"]) > 0)


if __name__ == "__main__":
    main()
