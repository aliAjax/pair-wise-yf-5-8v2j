"""社区天文台 —— 排队账与记录层。

负责数据的落盘与所有“改账”动作：
- 会员登记、复核恢复
- 预约登记（占不到名额即入候补，按先到先得排队）
- 取消（提前 2 小时不计爽约，之后记一次；累计两次自动暂停）
- 天气停开（记录保留并标 weather_cancelled；按停开范围补位/整体取消）
- 候补自动补位（暂停会员、同晚已有有效预约者顺延，原记录保留）
- 爽约登记
- 事件日志（审计）

数据存于单个 JSON 文件，写入用临时文件 + rename 原子替换，
服务重启后记录、判定状态与排队顺序都还在。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

import rules

# 预约状态
ST_WAITING = "waiting"                 # 候补中
ST_CONFIRMED = "confirmed"             # 已确认（占用当晚唯一时段）
ST_CANCELLED = "cancelled"             # 会员主动取消（不计/记爽约由 reason 细分）
ST_WEATHER = "weather_cancelled"       # 天气停开（保留原记录）
ST_NO_SHOW = "no_show"                 # 爽约

REASON_EARLY = "early_cancel"          # 提前 >=2h 取消，不计次
REASON_LATE = "late_cancel"           # 不足 2h 取消，记一次
REASON_WAITING = "waitlist_drop"      # 候补记录自行退出，不占名额、不计次
REASON_WEATHER = "weather"            # 天气停开
REASON_NO_SHOW = "no_show"            # 记爽约


class LedgerError(Exception):
    """业务错误：message 直接返回给调用方，code 为错误码。"""

    def __init__(self, message: str, code: str = "bad_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class Ledger:
    def __init__(self, path: str = "data/observatory.json"):
        self.path = path
        self._load()

    # ---------- 持久化 ----------

    def _empty(self) -> dict:
        return {
            "seq": {"member": 0, "booking": 0, "event": 0},
            "members": {},          # id -> member
            "bookings": [],         # 预约/候补记录（全部保留）
            "events": [],           # 审计事件
        }

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = self._empty()
            self._save()

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _next_id(self, kind: str, prefix: str) -> str:
        self.data["seq"][kind] += 1
        return f"{prefix}{self.data['seq'][kind]}"

    def _log(self, kind: str, detail: dict):
        eid = self._next_id("event", "E")
        event = {"id": eid, "at": datetime.now().isoformat(timespec="seconds"),
                 "kind": kind, **detail}
        self.data["events"].append(event)
        return event

    # ---------- 会员 ----------

    def register_member(self, name: str, contact: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise LedgerError("会员姓名不能为空", "empty_name")
        mid = self._next_id("member", "M")
        member = {
            "id": mid,
            "name": name,
            "contact": contact.strip(),
            "no_shows": 0,
            "suspended": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.data["members"][mid] = member
        self._log("member_registered", {"member": mid, "name": name})
        self._save()
        return member

    def get_member(self, member_id: str) -> dict | None:
        return self.data["members"].get(member_id)

    def _require_member(self, member_id: str) -> dict:
        m = self.get_member(member_id)
        if m is None:
            raise LedgerError(f"会员不存在：{member_id}", "member_not_found", 404)
        return m

    def reinstate_member(self, member_id: str, note: str = "") -> dict:
        """复核恢复：清掉暂停标记，爽约计数清零；未暂停也可复核。"""
        member = self._require_member(member_id)
        before = member["no_shows"]
        member["suspended"] = False
        member["no_shows"] = 0
        self._log("member_reinstated",
                  {"member": member_id, "no_shows_before": before, "note": note})
        self._save()
        return member

    # ---------- 查询 ----------

    def list_members(self) -> list:
        return sorted(self.data["members"].values(), key=lambda m: m["id"])

    def list_bookings(self, date_str: str | None = None, slot: str | None = None,
                      member_id: str | None = None, status: str | None = None) -> list:
        out = self.data["bookings"]
        if date_str:
            out = [b for b in out if b["date"] == date_str]
        if slot:
            out = [b for b in out if b["slot"] == slot]
        if member_id:
            out = [b for b in out if b["member"] == member_id]
        if status:
            out = [b for b in out if b["status"] == status]
        return out

    def get_booking(self, booking_id: str) -> dict | None:
        return next((b for b in self.data["bookings"] if b["id"] == booking_id), None)

    def _require_booking(self, booking_id: str) -> dict:
        b = self.get_booking(booking_id)
        if b is None:
            raise LedgerError(f"预约不存在：{booking_id}", "booking_not_found", 404)
        return b

    def _confirmed(self, date_str: str, slot: str) -> dict | None:
        return next((b for b in self.data["bookings"]
                     if b["date"] == date_str and b["slot"] == slot
                     and b["status"] == ST_CONFIRMED), None)

    def schedule(self, date_str: str) -> dict:
        """某晚的排期：每个时段的已确认者与候补队列。"""
        rules.parse_iso_date(date_str)
        slots = {}
        for slot in rules.DEFAULT_SLOTS:
            confirmed = next((b["id"] for b in self.data["bookings"]
                              if b["date"] == date_str and b["slot"] == slot
                              and b["status"] == ST_CONFIRMED), None)
            waitlist = [b["id"] for b in self._waitlist_ordered(date_str, slot)]
            slots[slot] = {"confirmed": confirmed, "waitlist": waitlist,
                           "available": confirmed is None}
        return {"date": date_str, "slots": slots}

    def events(self, limit: int = 100) -> list:
        return list(reversed(self.data["events"]))[:limit]

    def _waitlist_ordered(self, date_str: str, slot: str) -> list:
        """候补按登记先后（queued_at，相等再按 id）。"""
        return sorted(
            (b for b in self.data["bookings"]
             if b["date"] == date_str and b["slot"] == slot
             and b["status"] == ST_WAITING),
            key=lambda b: (b["queued_at"], b["id"]),
        )

    # ---------- 预约/候补 ----------

    def book(self, member_id: str, date_str: str, slot: str, target: str,
             now: datetime | None = None) -> dict:
        """登记会员+日期+时段+目标。名额空 -> confirmed；满 -> 入候补。"""
        member = self._require_member(member_id)
        rules.slot_start(date_str, slot)  # 校验日期与时段
        target = (target or "").strip()
        if not target:
            raise LedgerError("观测目标不能为空", "empty_target")
        now = now or datetime.now()

        ok, why = rules.can_request(member)
        if not ok:
            raise LedgerError(why, "member_suspended", 403)
        if rules.has_active_on_night(self.data["bookings"], member_id, date_str):
            raise LedgerError("同一会员每晚只能占一个时段（已有有效预约/候补）",
                              "one_slot_per_night", 409)

        bid = self._next_id("booking", "B")
        ts = now.isoformat(timespec="seconds")
        booking = {
            "id": bid,
            "member": member_id,
            "date": date_str,
            "slot": slot,
            "target": target,
            "status": ST_CONFIRMED,
            "reason": None,
            "created_at": ts,
            "queued_at": ts,
            "promoted_from_waitlist": False,
            "history": [{"at": ts, "status": ST_CONFIRMED}],
        }
        if self._confirmed(date_str, slot) is not None:
            # 望远镜该时段已被占：进候补队列
            booking["status"] = ST_WAITING
            booking["history"] = [{"at": ts, "status": ST_WAITING}]
            self.data["bookings"].append(booking)
            self._log("waitlisted",
                      {"booking": bid, "member": member_id, "date": date_str,
                       "slot": slot, "target": target})
            self._save()
            return booking

        self.data["bookings"].append(booking)
        self._log("booked",
                  {"booking": bid, "member": member_id, "date": date_str,
                   "slot": slot, "target": target})
        self._save()
        return booking

    def cancel(self, booking_id: str, now: datetime | None = None) -> tuple[dict, dict | None]:
        """会员取消自己的已确认预约：

        - 距时段开始 >=2 小时：记 early_cancel，不计爽约
        - 不足 2 小时：记 late_cancel，计一次爽约（累计两次自动暂停）
        取消后最早可补位的候补依次补上（暂停者顺延）。
        返回 (预约记录, 补位记录或 None)。
        """
        booking = self._require_booking(booking_id)
        if booking["status"] != ST_CONFIRMED:
            raise LedgerError(
                f"只有已确认预约可以取消，当前状态：{booking['status']}",
                "not_cancellable", 409)
        now = now or datetime.now()
        start = rules.slot_start(booking["date"], booking["slot"])
        kind = rules.cancellation_kind(start, now)
        if kind == "early":
            booking["status"], booking["reason"] = ST_CANCELLED, REASON_EARLY
            penalty = False
        else:
            booking["status"], booking["reason"] = ST_CANCELLED, REASON_LATE
            penalty = True

        booking["history"].append(
            {"at": now.isoformat(timespec="seconds"), "status": booking["status"],
             "reason": booking["reason"]})
        detail = {"booking": booking_id, "member": booking["member"],
                  "date": booking["date"], "slot": booking["slot"],
                  "early": not penalty}
        suspended = False
        if penalty:
            member = self._require_member(booking["member"])
            total, will_suspend = rules.next_no_show_state(member["no_shows"])
            member["no_shows"] = total
            member["suspended"] = will_suspend
            suspended = will_suspend
            detail["no_shows"] = total
        promoted = self._promote_next(booking["date"], booking["slot"], now)
        if promoted:
            detail["promoted"] = promoted["id"]
        self._log(
            "cancelled_early" if not penalty else "cancelled_late",
            {**detail, "suspended": suspended})
        self._save()
        return booking, promoted

    def drop_waiting(self, booking_id: str) -> dict:
        """候补者主动退出：不占名额、不计爽约，记录保留。"""
        booking = self._require_booking(booking_id)
        if booking["status"] != ST_WAITING:
            raise LedgerError("该记录不在候补中", "not_waiting", 409)
        ts = datetime.now().isoformat(timespec="seconds")
        booking["status"], booking["reason"] = ST_CANCELLED, REASON_WAITING
        booking["history"].append(
            {"at": ts, "status": ST_CANCELLED, "reason": REASON_WAITING})
        self._log("waitlist_dropped",
                  {"booking": booking_id, "member": booking["member"],
                   "date": booking["date"], "slot": booking["slot"]})
        self._save()
        return booking

    # ---------- 候补补位 ----------

    def _promote_next(self, date_str: str, slot: str, now: datetime) -> dict | None:
        """把该时段最早可补位的候补补成已确认；暂停/同晚已有预约者顺延。

        顺延者保留 waiting 记录。每个空位只补一位（符合“依次补上”）。
        """
        skipped = []
        promoted = None
        for entry in self._waitlist_ordered(date_str, slot):
            member = self.get_member(entry["member"])
            ok, why = rules.can_promote(member, self.data["bookings"], entry)
            if not ok:
                skipped.append({"booking": entry["id"], "member": entry["member"],
                                "reason": why})
                continue
            entry["status"] = ST_CONFIRMED
            entry["reason"] = None
            entry["promoted_from_waitlist"] = True
            entry["history"].append(
                {"at": now.isoformat(timespec="seconds"), "status": ST_CONFIRMED,
                 "reason": "promoted_from_waitlist"})
            promoted = entry
            break
        if promoted:
            self._log("promoted",
                      {"booking": promoted["id"], "member": promoted["member"],
                       "date": date_str, "slot": slot, "skipped": skipped})
        return promoted

    # ---------- 天气停开 ----------

    def close_slot(self, date_str: str, slot: str, reason_text: str = "") -> dict:
        """某晚某个时段天气停开：

        原已确认记录保留并标 weather_cancelled，随后最早可补位候补补上；
        其余候补继续排队。
        """
        rules.slot_start(date_str, slot)
        now = datetime.now()
        current = self._confirmed(date_str, slot)
        result = {"scope": "slot", "date": date_str, "slot": slot,
                  "cancelled": [], "promoted": None, "note": reason_text}
        if current is not None:
            current["status"], current["reason"] = ST_WEATHER, REASON_WEATHER
            current["history"].append(
                {"at": now.isoformat(timespec="seconds"), "status": ST_WEATHER,
                 "reason": REASON_WEATHER, "note": reason_text})
            result["cancelled"].append(current["id"])
            promoted = self._promote_next(date_str, slot, now)
            if promoted:
                result["promoted"] = promoted["id"]
        self._log("weather_close_slot", result)
        self._save()
        return result

    def close_night(self, date_str: str, reason_text: str = "") -> dict:
        """整晚天气停开：当晚所有已确认与候补记录保留并标 weather_cancelled，

        名额全部关闭，不进行补位。
        """
        rules.parse_iso_date(date_str)
        now = datetime.now()
        affected = []
        for b in self.data["bookings"]:
            if b["date"] == date_str and b["status"] in (ST_CONFIRMED, ST_WAITING):
                b["status"], b["reason"] = ST_WEATHER, REASON_WEATHER
                b["history"].append(
                    {"at": now.isoformat(timespec="seconds"), "status": ST_WEATHER,
                     "reason": REASON_WEATHER, "note": reason_text})
                affected.append(b["id"])
        result = {"scope": "night", "date": date_str, "cancelled": affected,
                  "promoted": None, "note": reason_text}
        self._log("weather_close_night", result)
        self._save()
        return result

    # ---------- 爽约 ----------

    def mark_no_show(self, booking_id: str, note: str = "") -> tuple[dict, dict]:
        """管理员登记爽约（会员未来）：记一次，累计两次自动暂停。"""
        booking = self._require_booking(booking_id)
        if booking["status"] != ST_CONFIRMED:
            raise LedgerError("只有已确认预约可以登记爽约", "not_confirmed", 409)
        now = datetime.now()
        booking["status"], booking["reason"] = ST_NO_SHOW, REASON_NO_SHOW
        booking["history"].append(
            {"at": now.isoformat(timespec="seconds"), "status": ST_NO_SHOW,
             "reason": REASON_NO_SHOW, "note": note})
        member = self._require_member(booking["member"])
        total, will_suspend = rules.next_no_show_state(member["no_shows"])
        member["no_shows"] = total
        member["suspended"] = will_suspend
        promoted = self._promote_next(booking["date"], booking["slot"], now)
        self._log("no_show",
                  {"booking": booking_id, "member": booking["member"],
                   "date": booking["date"], "slot": booking["slot"],
                   "no_shows": total, "suspended": will_suspend,
                   "promoted": promoted["id"] if promoted else None, "note": note})
        self._save()
        return booking, member
