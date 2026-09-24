"""判定逻辑：预约资格、取消判罚、爽约累计与停约复核。"""
from datetime import datetime, timedelta

# 一台望远镜，每晚固定四个时段
SLOTS = ["20:00-21:00", "21:00-22:00", "22:00-23:00", "23:00-24:00"]

FREE_CANCEL_HOURS = 2   # 提前两小时取消不计次数
NO_SHOW_LIMIT = 2       # 累计两次爽约暂停预约

ACTIVE_STATUSES = ("confirmed", "waitlisted")


def valid_date(date_str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def valid_slot(slot):
    return slot in SLOTS


def slot_start(date_str, slot):
    """时段开始的绝对时刻。"""
    start = slot.split("-")[0]
    return datetime.strptime("%s %s" % (date_str, start), "%Y-%m-%d %H:%M")


def slot_free(bookings, date, slot):
    """该时段当晚是否还没有正式预约。"""
    return not any(
        b["date"] == date and b["slot"] == slot and b["status"] == "confirmed"
        for b in bookings
    )


def check_booking_allowed(member, bookings, date, night):
    """预约资格判定。返回 None 表示允许，否则返回拒绝原因。"""
    if member["suspended"]:
        return "会员已被暂停预约，等待复核"
    if night and night.get("closed"):
        return "当晚因天气停开，暂不接受预约"
    for b in bookings:
        if (
            b["member_id"] == member["id"]
            and b["date"] == date
            and b["status"] in ACTIVE_STATUSES
        ):
            return "同一会员每晚只能占一个时段"
    return None


def judge_cancel(booking, now):
    """取消判罚：提前两小时及以上 -> 'free'；之后 -> 'no_show'。"""
    start = slot_start(booking["date"], booking["slot"])
    if now <= start - timedelta(hours=FREE_CANCEL_HOURS):
        return "free"
    return "no_show"


def apply_no_show(member):
    """记一次爽约；累计达到上限则暂停预约。返回是否被暂停。"""
    member["no_show_count"] += 1
    if member["no_show_count"] >= NO_SHOW_LIMIT:
        member["suspended"] = True
    return member["suspended"]


def reinstate(member):
    """复核通过：恢复预约资格并清零爽约次数。"""
    member["suspended"] = False
    member["no_show_count"] = 0
    return member
