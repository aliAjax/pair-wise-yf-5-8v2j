# 社区天文台望远镜预约服务

社区天文台只有一台望远镜。本服务登记会员、日期、时段和观测目标，处理预约、候补、取消判罚、天气停开与重开补位。零依赖，仅用 Python 3 标准库。

## 文件结构

| 文件 | 职责 |
|---|---|
| `server.py` | **请求入口**：HTTP 路由、参数解析、业务编排 |
| `rules.py` | **判定**：预约资格（每晚一个时段、停约状态）、取消两小时间隔判罚、爽约累计与复核恢复 |
| `waitlist.py` | **排队账**：候补队列登记、排位、空位依次补上（暂停会员顺延） |
| `store.py` | 持久化：JSON 文件原子写入，重启后记录仍在 |

每晚固定四个时段：`20:00-21:00`、`21:00-22:00`、`22:00-23:00`、`23:00-24:00`。

## 启动

```bash
python3 server.py                 # 默认端口 8000，数据文件 data.json
python3 server.py --port 9000 --data /var/lib/obs/data.json
```

数据实时写入数据文件，服务重启后记录自动恢复。

## 业务规则

- 同一会员每晚只能占一个时段（含候补）。
- 时段已被确认时，新预约进入候补队列，按登记先后排位。
- 取消：距时段开始 **≥2 小时**不计次数；**<2 小时**记爽约一次。累计 **2 次爽约**暂停预约资格，复核（review）后恢复并清零。
- 天气停开：当晚所有已确认预约**保留原记录**并标记 `cancelled_weather`，当晚不再接受新预约；**重开**时各时段空位由最早候补依次补上。
- 补位时遇到已暂停会员，**顺延给后一位**，暂停会员保留队列位置，复核恢复后仍可被补上。

## 接口调用说明

所有请求/响应均为 JSON。涉及时间判断的接口（预约、取消、停开、重开）支持在请求体传 `"now": "YYYY-MM-DDTHH:MM:SS"` 注入当前时刻，便于演示与测试；不传则取服务器当前时间。

### 会员

```bash
# 登记会员
curl -X POST localhost:8000/members -d '{"name":"小张"}'

# 会员列表
curl localhost:8000/members

# 复核：恢复被暂停会员的预约资格，爽约清零
curl -X POST localhost:8000/members/m0001/review
```

### 预约与取消

```bash
# 预约（时段空闲则 confirmed，否则 waitlisted 并返回 queue_position）
curl -X POST localhost:8000/bookings \
  -d '{"member_id":"m0001","date":"2026-09-25","slot":"20:00-21:00","target":"M31 仙女座星系"}'

# 查询预约（可按 date / member_id / status 过滤）
curl "localhost:8000/bookings?date=2026-09-25&status=confirmed"

# 取消：提前两小时以上 -> penalty: none；两小时内 -> penalty: no_show
# 取消后时段空出，最早候补自动补上（响应中 promoted 字段）
curl -X POST localhost:8000/bookings/b0001/cancel -d '{"now":"2026-09-25T17:00:00"}'
```

### 天气停开与重开

```bash
# 天气停开：已确认预约标记 cancelled_weather（记录保留），当晚关闭
curl -X POST localhost:8000/nights/2026-09-25/weather-close -d '{"reason":"台风预警"}'

# 重开：当晚恢复开放，各时段空位由最早候补依次补上（暂停会员顺延）
curl -X POST localhost:8000/nights/2026-09-25/reopen -d '{}'

# 当晚视图：各时段占用与候补队列
curl localhost:8000/nights/2026-09-25
```

### 预约状态说明

| 状态 | 含义 |
|---|---|
| `confirmed` | 已确认，占用时段 |
| `waitlisted` | 候补中 |
| `cancelled` | 提前两小时以上取消 / 候补主动取消，不计次数 |
| `cancelled_late` | 两小时内取消，记爽约一次 |
| `cancelled_weather` | 天气停开被取消，记录保留 |

## 典型流程示例

```bash
# 1. 三人登记，小张确认 20 点档，小李、小王依次候补
# 2. 小张 17:00 取消（20 点开始，提前 3 小时）-> 不计次数，小李补上
# 3. 小李 19:00 取消（提前 1 小时）-> 记爽约一次，小王补上
# 4. 小李再次两小时内取消 -> 爽约累计 2 次，暂停预约
# 5. 此后小李若在候补队列最前，补位时顺延给后一位
# 6. POST /members/m0002/review 复核后恢复，爽约清零
# 7. 某晚天气停开 -> 确认记录标记 cancelled_weather；重开 -> 候补依次补上
# 8. 重启服务 -> 以上记录全部保留
```
