"""请求入口：HTTP 路由与参数解析。

业务规则判定在 rules.py，候补排队账在 waitlist.py，持久化在 store.py。
启动：python3 server.py [--port 8000] [--data data.json]
"""
import json
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import rules
import waitlist
from store import Store


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_now(value):
    """支持请求体带 "now"（ISO 格式）注入当前时刻，便于演示与测试。"""
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ApiError(400, "now 格式应为 ISO，如 2026-09-24T19:30:00")
    return datetime.now()


# ---------------------------------------------------------------- 业务编排

def create_member(store, payload):
    name = (payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, "会员姓名不能为空")
    with store.lock:
        member = {
            "id": store.next_id("member", "m"),
            "name": name,
            "no_show_count": 0,
            "suspended": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        store.data["members"][member["id"]] = member
        store.save()
        return 201, member


def review_member(store, member_id):
    """复核：恢复被暂停会员的预约资格。"""
    with store.lock:
        member = store.data["members"].get(member_id)
        if not member:
            raise ApiError(404, "会员不存在")
        was_suspended = member["suspended"]
        rules.reinstate(member)
        store.save()
        return 200, {
            "member": member,
            "note": "复核通过，已恢复预约资格" if was_suspended else "会员未处于暂停状态",
        }


def create_booking(store, payload):
    member_id = payload.get("member_id")
    date = payload.get("date")
    slot = payload.get("slot")
    target = (payload.get("target") or "").strip()
    now = parse_now(payload.get("now"))

    if not rules.valid_date(date):
        raise ApiError(400, "日期格式应为 YYYY-MM-DD")
    if not rules.valid_slot(slot):
        raise ApiError(400, "时段无效，可选: " + ", ".join(rules.SLOTS))
    if not target:
        raise ApiError(400, "观测目标不能为空")

    with store.lock:
        member = store.data["members"].get(member_id)
        if not member:
            raise ApiError(404, "会员不存在")
        night = store.data["nights"].get(date)
        all_bookings = list(store.data["bookings"].values())

        err = rules.check_booking_allowed(member, all_bookings, date, night)
        if err:
            raise ApiError(409, err)

        status = "confirmed" if rules.slot_free(all_bookings, date, slot) else "waitlisted"
        booking = {
            "id": store.next_id("booking", "b"),
            "seq": store.next_seq(),
            "member_id": member_id,
            "date": date,
            "slot": slot,
            "target": target,
            "status": status,
            "created_at": now.isoformat(timespec="seconds"),
            "cancelled_at": None,
            "cancel_reason": None,
            "promoted_at": None,
        }
        store.data["bookings"][booking["id"]] = booking
        store.save()

        resp = dict(booking)
        if status == "waitlisted":
            resp["queue_position"] = waitlist.position(all_bookings + [booking], booking)
        return 201, resp


def cancel_booking(store, booking_id, payload):
    now = parse_now(payload.get("now"))
    with store.lock:
        booking = store.data["bookings"].get(booking_id)
        if not booking:
            raise ApiError(404, "预约不存在")
        if booking["status"] not in rules.ACTIVE_STATUSES:
            raise ApiError(409, "该预约已取消或已结束")

        member = store.data["members"][booking["member_id"]]
        penalty = "none"
        promoted = None
        skipped = []

        if booking["status"] == "confirmed":
            if rules.judge_cancel(booking, now) == "free":
                booking["status"] = "cancelled"
                booking["cancel_reason"] = "提前两小时以上取消，不计次数"
            else:
                booking["status"] = "cancelled_late"
                booking["cancel_reason"] = "两小时内取消，记爽约一次"
                penalty = "no_show"
                rules.apply_no_show(member)
            booking["cancelled_at"] = now.isoformat(timespec="seconds")
            # 时段空出：当晚未停开则最早候补依次补上
            night = store.data["nights"].get(booking["date"])
            if not (night and night.get("closed")):
                promoted, skipped = waitlist.promote(
                    store.data, booking["date"], booking["slot"], now
                )
        else:  # 候补主动取消，不涉及判罚
            booking["status"] = "cancelled"
            booking["cancel_reason"] = "候补主动取消"
            booking["cancelled_at"] = now.isoformat(timespec="seconds")

        store.save()
        return 200, {
            "booking": booking,
            "penalty": penalty,
            "member": member,
            "promoted": promoted,
            "skipped_suspended": skipped,
        }


def weather_close(store, date, payload):
    """天气停开：保留原记录并标成取消，当晚关闭预约。"""
    if not rules.valid_date(date):
        raise ApiError(400, "日期格式应为 YYYY-MM-DD")
    reason = (payload.get("reason") or "天气停开").strip()
    now = parse_now(payload.get("now"))
    with store.lock:
        night = store.data["nights"].setdefault(
            date, {"date": date, "closed": False, "close_reason": None}
        )
        night["closed"] = True
        night["close_reason"] = reason
        cancelled = []
        for b in store.data["bookings"].values():
            if b["date"] == date and b["status"] == "confirmed":
                b["status"] = "cancelled_weather"
                b["cancel_reason"] = reason
                b["cancelled_at"] = now.isoformat(timespec="seconds")
                cancelled.append(b["id"])
        store.save()
        return 200, {
            "date": date,
            "closed": True,
            "cancelled": cancelled,
            "note": "原记录已保留并标记为取消；重开后最早候补依次补上",
        }


def reopen_night(store, date, payload):
    """重开：当晚恢复开放，各时段空位由最早候补依次补上（暂停会员顺延）。"""
    if not rules.valid_date(date):
        raise ApiError(400, "日期格式应为 YYYY-MM-DD")
    now = parse_now(payload.get("now"))
    with store.lock:
        night = store.data["nights"].setdefault(
            date, {"date": date, "closed": False, "close_reason": None}
        )
        night["closed"] = False
        night["close_reason"] = None
        promoted, skipped = [], []
        for slot in rules.SLOTS:
            p, sk = waitlist.promote(store.data, date, slot, now)
            if p:
                promoted.append(p)
            skipped.extend(sk)
        store.save()
        return 200, {
            "date": date,
            "closed": False,
            "promoted": promoted,
            "skipped_suspended": sorted(set(skipped)),
        }


def night_view(store, date):
    if not rules.valid_date(date):
        raise ApiError(400, "日期格式应为 YYYY-MM-DD")
    with store.lock:
        night = store.data["nights"].get(date) or {"date": date, "closed": False}
        all_bookings = list(store.data["bookings"].values())
        slots = {}
        for slot in rules.SLOTS:
            conf = next(
                (
                    b
                    for b in all_bookings
                    if b["date"] == date and b["slot"] == slot and b["status"] == "confirmed"
                ),
                None,
            )
            q = waitlist.queue_for(all_bookings, date, slot)
            slots[slot] = {
                "state": "confirmed" if conf else "free",
                "booking": conf,
                "waitlist": [
                    {"position": i, "booking_id": b["id"], "member_id": b["member_id"],
                     "target": b["target"]}
                    for i, b in enumerate(q, 1)
                ],
            }
        return 200, {"date": date, "closed": night.get("closed", False), "slots": slots}


def list_bookings(store, query):
    with store.lock:
        result = list(store.data["bookings"].values())
        for key in ("date", "member_id", "status"):
            if query.get(key):
                result = [b for b in result if b[key] == query[key][0]]
        result.sort(key=lambda b: b["seq"])
        return 200, result


# ---------------------------------------------------------------- 路由

ROUTES = [
    ("GET", r"^/$", lambda s, p, q, b: (200, {
        "service": "社区天文台望远镜预约",
        "endpoints": [
            "POST /members {name}",
            "GET  /members",
            "POST /members/{id}/review            复核恢复",
            "POST /bookings {member_id,date,slot,target[,now]}",
            "GET  /bookings?date=&member_id=&status=",
            "POST /bookings/{id}/cancel {now?}    两小时内取消记爽约",
            "GET  /nights/{date}                  当晚时段与候补",
            "POST /nights/{date}/weather-close    天气停开",
            "POST /nights/{date}/reopen           重开并补位",
        ],
    })),
    ("POST", r"^/members$", lambda s, p, q, b: create_member(s, b)),
    ("GET", r"^/members$", lambda s, p, q, b: (200, list(s.data["members"].values()))),
    ("POST", r"^/members/([^/]+)/review$", lambda s, p, q, b: review_member(s, p[0])),
    ("POST", r"^/bookings$", lambda s, p, q, b: create_booking(s, b)),
    ("GET", r"^/bookings$", lambda s, p, q, b: list_bookings(s, q)),
    ("POST", r"^/bookings/([^/]+)/cancel$", lambda s, p, q, b: cancel_booking(s, p[0], b)),
    ("GET", r"^/nights/([^/]+)$", lambda s, p, q, b: night_view(s, p[0])),
    ("POST", r"^/nights/([^/]+)/weather-close$", lambda s, p, q, b: weather_close(s, p[0], b)),
    ("POST", r"^/nights/([^/]+)/reopen$", lambda s, p, q, b: reopen_night(s, p[0], b)),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Observatory/1.0"
    store = None  # 启动时注入

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(400, "请求体不是合法 JSON")

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            body = self._body() if method == "POST" else {}
            for m, pattern, fn in ROUTES:
                match = re.match(pattern, parsed.path)
                if m == method and match:
                    status, obj = fn(self.store, list(match.groups()), parse_qs(parsed.query), body)
                    self._send(status, obj)
                    return
            raise ApiError(404, "接口不存在")
        except ApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": "服务器内部错误: %s" % e})

    do_GET = lambda self: self._dispatch("GET")
    do_POST = lambda self: self._dispatch("POST")

    def log_message(self, fmt, *args):
        sys.stderr.write("[obs] " + fmt % args + "\n")


def main():
    port, data_path = 8000, "data.json"
    args = sys.argv[1:]
    while args:
        flag, value = args.pop(0), args.pop(0)
        if flag == "--port":
            port = int(value)
        elif flag == "--data":
            data_path = value
        else:
            raise SystemExit("未知参数: %s" % flag)
    Handler.store = Store(data_path)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("社区天文台预约服务已启动: http://127.0.0.1:%d (数据文件: %s)" % (port, data_path))
    server.serve_forever()


if __name__ == "__main__":
    main()
