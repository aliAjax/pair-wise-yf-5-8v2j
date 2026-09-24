"""判定层 rules.py 的纯函数单元测试（无需起服务）。

运行：python3 rules_test.py
"""

from datetime import datetime

import rules

fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}", detail if not cond else "")
    if not cond:
        fails.append(name)


# 时段解析与校验
s = rules.slot_start("2026-09-25", "20:00")
check("时段开始时刻正确", s == datetime(2026, 9, 25, 20, 0), s)

bad = False
try:
    rules.slot_start("2026-09-25", "25:00")
except ValueError:
    bad = True
check("非法时段报错", bad)

bad = False
try:
    rules.slot_start("2026/09/25", "20:00")
except ValueError:
    bad = True
check("非法日期报错", bad)

# 两小时边界：>=2h 不计，<2h 计
check("提前 3 小时 -> early",
      rules.cancellation_kind(s, datetime(2026, 9, 25, 17, 0)) == "early")
check("正好提前 2 小时 -> early（边界含 2 小时）",
      rules.cancellation_kind(s, datetime(2026, 9, 25, 18, 0)) == "early")
check("提前 1 小时 59 分 -> late",
      rules.cancellation_kind(s, datetime(2026, 9, 25, 18, 1)) == "late")
check("开始之后才取消 -> late",
      rules.cancellation_kind(s, datetime(2026, 9, 25, 20, 30)) == "late")

# 会员资格
check("暂停会员不可新约",
      rules.can_request({"id": "M1", "suspended": True}) == (False, "会员已被暂停预约，需复核恢复后再约"))
check("正常会员可新约", rules.can_request({"id": "M1", "suspended": False})[0])
check("不存在会员不可新约", rules.can_request(None)[0] is False)

# 爽约计数 -> 两次暂停
check("第 1 次爽约不暂停", rules.next_no_show_state(0) == (1, False))
check("第 2 次爽约暂停", rules.next_no_show_state(1) == (2, True))

# 同晚唯一有效时段
bookings = [
    {"id": "B1", "member": "M1", "date": "D", "status": "confirmed"},
    {"id": "B2", "member": "M1", "date": "D", "status": "cancelled"},
    {"id": "B3", "member": "M1", "date": "other", "status": "waiting"},
]
check("同晚有 confirmed 算占用", rules.has_active_on_night(bookings, "M1", "D"))
check("取消记录不算占用（排除自身）",
      not rules.has_active_on_night(
          [{**bookings[0], "status": "weather_cancelled"}], "M1", "D"))
check("不同晚不冲突", not rules.has_active_on_night(bookings, "M1", "D2"))

# 候补补位资格
m = {"id": "M9", "suspended": True}
entry = {"id": "B9", "member": "M9", "date": "D"}
ok, why = rules.can_promote(m, [], entry)
check("暂停候补顺延", not ok and "顺延" in why, (ok, why))
entry2 = {"id": "B10", "member": "M1", "date": "D"}
check("同晚已有有效预约的候补顺延",
      rules.can_promote({"id": "M1", "suspended": False}, bookings, entry2)[0] is False)
check("正常候补可补位",
      rules.can_promote({"id": "M2", "suspended": False}, bookings,
                        {"id": "B11", "member": "M2", "date": "D"})[0])

print()
if fails:
    print(f"{len(fails)} 项失败：{fails}")
    raise SystemExit(1)
print("规则单元测试全部通过 ✓")
