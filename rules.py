"""社区天文台 —— 业务判定层（纯规则，无 IO、无状态）。

本文件只负责回答“合不合规、算哪种情况”，不读写数据、不处理 HTTP：
- 时段解析与校验
- 提前两小时取消的判定（early / late）
- 会员是否可预约、是否可候补补位
- 同一会员每晚仅一个有效时段的判定

排队/记录见 ledger.py，请求入口见 api.py。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# 每晚开放的固定时段（望远镜一台，每个时段仅一个名额）
DEFAULT_SLOTS = ("19:00", "20:00", "21:00", "22:00")
SLOT_MINUTES = 60

# 时段开始前 >=2 小时取消不计爽约；之后取消记一次
CANCEL_FREE_HOURS = 2

# 累计爽约次数达到该值即暂停预约，复核（reinstate）后恢复
SUSPEND_THRESHOLD = 2

# 有效（占用名额/占用当晚资格）的预约状态
ACTIVE_STATUSES = ("waiting", "confirmed")
# 终态：记录保留但不再占名额
CANCEL_STATUSES = ("cancelled", "weather_cancelled", "no_show")


def parse_iso_date(value: str) -> date:
    """解析 YYYY-MM-DD。"""
    if not isinstance(value, str):
        raise ValueError("日期必须是 YYYY-MM-DD 字符串")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def slot_start(date_str: str, slot: str, slots=DEFAULT_SLOTS) -> datetime:
    """返回某晚某个时段的开始时刻；非法时段抛 ValueError。"""
    day = parse_iso_date(date_str)
    if slot not in slots:
        raise ValueError(f"未知时段 {slot!r}，可选：{', '.join(slots)}")
    hour, minute = (int(x) for x in slot.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute)


def is_valid_slot(slot: str, slots=DEFAULT_SLOTS) -> bool:
    return slot in slots


def cancellation_kind(start: datetime, now: datetime) -> str:
    """取消时刻相对时段开始的判定：

    - "early"：距开始 >= 2 小时（含正好 2 小时），不计爽约
    - "late" ：距开始不足 2 小时（含开始之后），记一次爽约
    """
    return "early" if start - now >= timedelta(hours=CANCEL_FREE_HOURS) else "late"


def member_suspended(member: dict | None) -> bool:
    return bool(member and member.get("suspended"))


def has_active_on_night(bookings, member_id: str, date_str: str, exclude_id: str | None = None) -> bool:
    """同一会员同一晚是否已有有效（候补/已确认）预约。

    已取消/天气取消/爽约的历史记录保留，但不再占用当晚资格。
    """
    for b in bookings:
        if b["id"] == exclude_id:
            continue
        if b["member"] != member_id or b["date"] != date_str:
            continue
        if b["status"] in ACTIVE_STATUSES:
            return True
    return False


def can_request(member: dict | None) -> tuple[bool, str]:
    """新预约/新候补资格：会员存在且未被暂停。"""
    if member is None:
        return False, "会员不存在"
    if member_suspended(member):
        return False, "会员已被暂停预约，需复核恢复后再约"
    return True, ""


def can_promote(member: dict | None, bookings, entry: dict) -> tuple[bool, str]:
    """候补补位资格（排队账依次叫号时调用）：

    - 会员存在且未暂停（暂停者顺延给后一位，其候补保留）
    - 同晚没有其它有效预约
    """
    if member is None:
        return False, "会员不存在"
    if member_suspended(member):
        return False, "会员暂停中，顺延"
    if has_active_on_night(bookings, member["id"], entry["date"], exclude_id=entry["id"]):
        return False, "同晚已有有效预约，顺延"
    return True, ""


def next_no_show_state(no_shows: int) -> tuple[int, bool]:
    """再记一次爽约后的 (累计次数, 是否暂停)。"""
    total = no_shows + 1
    return total, total >= SUSPEND_THRESHOLD
