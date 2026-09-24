"""排队账：候补队列的登记、排位与依次补位。"""
import rules


def queue_for(bookings, date, slot):
    """某夜某时段的候补队列，按登记先后（seq）排序。"""
    q = [
        b
        for b in bookings
        if b["date"] == date and b["slot"] == slot and b["status"] == "waitlisted"
    ]
    return sorted(q, key=lambda b: b["seq"])


def position(bookings, booking):
    """该候补在队列中的位次（1 起）；非候补返回 None。"""
    if booking["status"] != "waitlisted":
        return None
    q = queue_for(bookings, booking["date"], booking["slot"])
    for i, b in enumerate(q, 1):
        if b["id"] == booking["id"]:
            return i
    return None


def _member_confirmed_that_night(bookings, member_id, date):
    return any(
        b["member_id"] == member_id and b["date"] == date and b["status"] == "confirmed"
        for b in bookings
    )


def promote(data, date, slot, now):
    """时段空出时依次补位：最早候补优先，遇到暂停会员顺延给后一位。

    返回 (被补上的预约或 None, 被顺延跳过的会员 id 列表)。
    """
    bookings = data["bookings"]
    members = data["members"]
    if not rules.slot_free(list(bookings.values()), date, slot):
        return None, []
    skipped = []
    for b in queue_for(list(bookings.values()), date, slot):
        member = members.get(b["member_id"])
        if member is None:
            continue
        if member["suspended"]:
            skipped.append(member["id"])  # 暂停会员：顺延，但保留其队列位置
            continue
        if _member_confirmed_that_night(list(bookings.values()), member["id"], date):
            continue  # 防御：同一会员每晚只能占一个时段
        b["status"] = "confirmed"
        b["promoted_at"] = now.isoformat(timespec="seconds")
        return b, skipped
    return None, skipped
