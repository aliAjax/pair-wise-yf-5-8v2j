"""社区天文台 —— HTTP 请求入口（标准库实现，无第三方依赖）。

启动：python3 api.py
数据：默认 data/observatory.json（可用环境变量 OBS_DATA 覆盖，重启记录仍在）
端口：默认 8000（可用 OBS_PORT 覆盖）

规则判定见 rules.py，排队账/记录见 ledger.py。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ledger import Ledger, LedgerError

DATA_PATH = os.environ.get("OBS_DATA", os.path.join("data", "observatory.json"))

# 业务错误码 -> HTTP 状态码
_HTTP_STATUS = {
    "bad_request": 400, "empty_name": 400, "empty_target": 400,
    "one_slot_per_night": 409, "member_suspended": 403,
    "not_cancellable": 409, "not_waiting": 409, "not_confirmed": 409,
    "member_not_found": 404, "booking_not_found": 404,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "CommunityObservatory/1.0"
    ledger: Ledger = None  # 由 main 注入，所有请求共用同一份账

    # ---- 工具 ----

    def _send(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, code: str = "bad_request", status: int = 400):
        self._send({"error": {"code": code, "message": message}}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise LedgerError("请求体必须是合法 JSON", "bad_request")
        if not isinstance(data, dict):
            raise LedgerError("请求体必须是 JSON 对象", "bad_request")
        return data

    def _require(self, data: dict, key: str) -> str:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise LedgerError(f"缺少必填字段：{key}", "bad_request")
        return value

    def _parse_now(self, data: dict) -> datetime | None:
        """可选 now 字段（ISO 8601），用于演示/测试两小时判定；不传则用当前时刻。"""
        raw = data.get("now")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            raise LedgerError("now 必须是 ISO 8601 时间，如 2026-09-25T17:30", "bad_request")

    def log_message(self, fmt, *args):  # 简洁日志
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}")

    # ---- 路由 ----

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path, qs = parsed.path.rstrip("/") or "/", parse_qs(parsed.query)
            if path == "/":
                return self._index()
            if path == "/health":
                return self._send({"status": "ok"})
            if path == "/members":
                return self._send({"members": self.ledger.list_members()})
            if path == "/bookings":
                return self._send({"bookings": self.ledger.list_bookings(
                    date_str=(qs.get("date") or [None])[0],
                    slot=(qs.get("slot") or [None])[0],
                    member_id=(qs.get("member") or [None])[0],
                    status=(qs.get("status") or [None])[0])})
            if path == "/events":
                limit = int((qs.get("limit") or ["100"])[0])
                return self._send({"events": self.ledger.events(limit)})
            if path.startswith("/schedule/"):
                return self._send(self.ledger.schedule(path.rsplit("/", 1)[1]))
            if path.startswith("/bookings/"):
                booking = self.ledger.get_booking(path.rsplit("/", 1)[1])
                if booking is None:
                    return self._error("预约不存在", "booking_not_found", 404)
                return self._send(booking)
            if path.startswith("/members/"):
                member = self.ledger.get_member(path.rsplit("/", 1)[1])
                if member is None:
                    return self._error("会员不存在", "member_not_found", 404)
                return self._send(member)
            return self._error("没有这个接口", "not_found", 404)
        except LedgerError as e:
            return self._error(str(e), e.code, e.status)
        except ValueError as e:
            return self._error(str(e), "bad_request", 400)
        except Exception as e:  # noqa: BLE001
            return self._error(f"服务器内部错误：{e}", "internal", 500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            data = self._read_json()

            if path == "/members":
                m = self.ledger.register_member(self._require(data, "name"),
                                                data.get("contact", ""))
                return self._send(m, 201)

            if path == "/bookings":
                b = self.ledger.book(
                    self._require(data, "member"), self._require(data, "date"),
                    self._require(data, "slot"), self._require(data, "target"),
                    now=self._parse_now(data))
                return self._send(b, 201)

            if path.startswith("/bookings/") and path.endswith("/cancel"):
                bid = path.split("/")[2]
                booking, promoted = self.ledger.cancel(bid, now=self._parse_now(data))
                return self._send({"booking": booking,
                                   "promoted": promoted})

            if path.startswith("/bookings/") and path.endswith("/drop-waiting"):
                booking = self.ledger.drop_waiting(path.split("/")[2])
                return self._send(booking)

            if path.startswith("/bookings/") and path.endswith("/no-show"):
                bid = path.split("/")[2]
                booking, member = self.ledger.mark_no_show(bid, data.get("note", ""))
                return self._send({"booking": booking, "member": member})

            if path.startswith("/members/") and path.endswith("/reinstate"):
                member = self.ledger.reinstate_member(path.split("/")[2],
                                                      data.get("note", ""))
                return self._send(member)

            if path == "/weather/close-slot":
                return self._send(self.ledger.close_slot(
                    self._require(data, "date"), self._require(data, "slot"),
                    data.get("note", "")))

            if path == "/weather/close-night":
                return self._send(self.ledger.close_night(
                    self._require(data, "date"), data.get("note", "")))

            return self._error("没有这个接口", "not_found", 404)
        except LedgerError as e:
            return self._error(str(e), e.code, e.status)
        except ValueError as e:
            return self._error(str(e), "bad_request", 400)
        except Exception as e:  # noqa: BLE001
            return self._error(f"服务器内部错误：{e}", "internal", 500)

    def _index(self):
        return self._send({
            "service": "community-observatory",
            "telescope": "one telescope, one slot per night per member",
            "slots": list(__import__("rules").DEFAULT_SLOTS),
            "endpoints": {
                "POST /members": {"body": {"name": "姓名", "contact": "可选"}},
                "GET /members": "会员列表",
                "GET /members/{id}": "会员详情（含爽约次数/暂停状态）",
                "POST /members/{id}/reinstate": {"body": {"note": "复核说明"}},
                "POST /bookings": {
                    "body": {"member": "M1", "date": "2026-09-25",
                             "slot": "20:00", "target": "M31"}},
                "GET /bookings": "?date=&slot=&member=&status= 过滤",
                "GET /bookings/{id}": "预约详情（记录全部保留）",
                "POST /bookings/{id}/cancel": {
                    "body": {"now": "可选 ISO8601，不传取当前时间"}},
                "POST /bookings/{id}/drop-waiting": "候补退出（不计爽约）",
                "POST /bookings/{id}/no-show": {"body": {"note": "可选"}},
                "GET /schedule/{date}": "当晚各时段 confirmed + 候补队列",
                "POST /weather/close-slot": {
                    "body": {"date": "2026-09-25", "slot": "20:00", "note": "可选"}},
                "POST /weather/close-night": {"body": {"date": "2026-09-25"}},
                "GET /events?limit=100": "审计事件（最新在前）",
                "GET /health": "健康检查",
            },
        })


def main(host: str = "0.0.0.0", port: int | None = None):
    port = port or int(os.environ.get("OBS_PORT", "8000"))
    Handler.ledger = Ledger(DATA_PATH)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"社区天文台预约服务已启动： http://127.0.0.1:{port}")
    print(f"数据文件： {os.path.abspath(DATA_PATH)}（重开后记录仍在）")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
