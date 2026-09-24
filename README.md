# 社区天文台望远镜预约服务

社区只有一台望远镜。本服务登记 **会员、日期、时段、观测目标**，并实现候补排队、
天气停开、两小时取消规则、爽约暂停与复核恢复。

- 只依赖 Python 3 标准库，无需安装任何第三方包
- 数据落盘为单个 JSON 文件（默认 `data/observatory.json`），临时文件 + 原子替换写入
- **服务重开后记录、排队顺序、暂停/爽约状态都还在**

## 文件结构（三类业务分离）

| 文件 | 职责 |
| --- | --- |
| `rules.py` | **判定**：纯业务规则（时段校验、两小时边界、同晚唯一资格、暂停与补位资格、爽约阈值），无 IO、无状态 |
| `ledger.py` | **排队账与记录**：会员/预约/候补的存取、候补队列叫号、天气停开、爽约记账、事件日志、JSON 持久化 |
| `api.py` | **请求入口**：HTTP 路由、JSON 解析、错误码；本身不含业务判定 |
| `rules_test.py` | 判定层单元测试 |
| `smoke_test.py` | 端到端冒烟测试（真实起服、含重启持久化校验） |

## 启动

```bash
python3 api.py
# 社区天文台预约服务已启动： http://127.0.0.1:8000
```

可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `OBS_PORT` | `8000` | 监听端口 |
| `OBS_DATA` | `data/observatory.json` | 数据文件路径（首次启动自动创建） |

健康检查：

```bash
curl http://127.0.0.1:8000/health
# {"status": "ok"}
```

浏览器或 `curl http://127.0.0.1:8000/` 可看到全部接口清单。

每晚固定时段（每时段仅 1 个名额，即望远镜一台）：
`19:00`、`20:00`、`21:00`、`22:00`。

## 业务规则

1. **登记**：会员、日期（`YYYY-MM-DD`）、时段、目标四要素。
2. **同一会员每晚只能占一个时段**：已有「候补/已确认」记录时再约返回 `409`；
   取消/天气取消/爽约的历史记录保留，但不再占用当晚资格。不同晚不冲突。
3. **候补**：某时段名额已满时自动进入该时段候补队列，按登记先后排队。
4. **取消（已确认预约）**：
   - 距时段开始 **≥ 2 小时**取消：`early_cancel`，**不计**爽约；
   - 距时段开始 **< 2 小时**取消：`late_cancel`，**记一次爽约**。
   - 取消后该时段**最早可补位的候补自动补上**；暂停会员、同晚已有有效预约者
     **顺延给后一位**，其候补记录保留在队列中。
5. **暂停与恢复**：爽约（晚取消或登记爽约）**累计两次自动暂停**；暂停期间不能
   新预约/新候补。管理员复核后恢复：清暂停标记并把爽约计数清零。
6. **天气停开（原记录保留并标 `weather_cancelled`，不删数据、不计个人爽约）**：
   - `POST /weather/close-slot`：单个时段停开，原确认者标记取消，**最早候补补上**；
   - `POST /weather/close-night`：整晚停开，当晚确认与候补**全部标记取消，不补位**
     （整晚无观测名额）。
7. **候补退出**：候补者主动退出不占名额、**不计爽约**，记录保留。
8. 所有改账动作写入 `/events` 审计日志。

## 接口调用示例

### 1. 登记会员

```bash
curl -s -X POST http://127.0.0.1:8000/members \
  -H 'Content-Type: application/json' \
  -d '{"name":"阿威","contact":"a@example.com"}'
# 返回 {"id":"M1", "no_shows":0, "suspended":false, ...}
```

### 2. 预约（名额空 = 已确认；满 = 自动候补）

```bash
curl -s -X POST http://127.0.0.1:8000/bookings \
  -H 'Content-Type: application/json' \
  -d '{"member":"M1","date":"2026-09-25","slot":"20:00","target":"M31 仙女座"}'
# status: "confirmed"；同晚再约别的时段 -> 409 one_slot_per_night
# 该时段已有 confirmed 时 -> status: "waiting"（进入候补）
```

### 3. 查看当晚排期（已确认者 + 候补队列）

```bash
curl -s http://127.0.0.1:8000/schedule/2026-09-25
```

### 4. 取消（按当前时间判定两小时）

```bash
curl -s -X POST http://127.0.0.1:8000/bookings/B1/cancel
# 返回 {"booking": {... "reason":"early_cancel|late_cancel"}, "promoted": {补位者}|null}
```

> 调试/演示时可在请求体传 ISO 8601 的 `"now"` 来固定判定时刻：
> `.../cancel -d '{"now":"2026-09-25T17:30"}'`。

### 5. 候补退出（不计爽约）

```bash
curl -s -X POST http://127.0.0.1:8000/bookings/B2/drop-waiting
```

### 6. 天气停开

```bash
# 单个时段：原记录标记 weather_cancelled，最早候补补上
curl -s -X POST http://127.0.0.1:8000/weather/close-slot \
  -H 'Content-Type: application/json' \
  -d '{"date":"2026-09-25","slot":"20:00","note":"雷暴"}'

# 整晚停开：全部标记取消，不补位
curl -s -X POST http://127.0.0.1:8000/weather/close-night \
  -H 'Content-Type: application/json' \
  -d '{"date":"2026-09-25","note":"台风"}'
```

### 7. 爽约登记（会员没来）、复核恢复

```bash
curl -s -X POST http://127.0.0.1:8000/bookings/B3/no-show -d '{"note":"未到场"}'
# 累计第 2 次时返回的 member.suspended = true

curl -s -X POST http://127.0.0.1:8000/members/M2/reinstate -d '{"note":"已电话核实"}'
# suspended=false, no_shows=0，恢复预约
```

### 8. 查询

```bash
curl -s 'http://127.0.0.1:8000/members'
curl -s 'http://127.0.0.1:8000/members/M1'
curl -s 'http://127.0.0.1:8000/bookings?date=2026-09-25'
curl -s 'http://127.0.0.1:8000/bookings?member=M1&status=weather_cancelled'
curl -s 'http://127.0.0.1:8000/bookings/B1'
curl -s 'http://127.0.0.1:8000/events?limit=50'
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/members` | 登记会员 `{name, contact?}` |
| GET | `/members`、`/members/{id}` | 会员列表/详情（含 `no_shows`、`suspended`） |
| POST | `/members/{id}/reinstate` | 复核恢复，爽约计数清零 |
| POST | `/bookings` | 预约/候补 `{member,date,slot,target}` |
| GET | `/bookings` | 查询，支持 `date/slot/member/status` 过滤 |
| GET | `/bookings/{id}` | 预约详情（含状态变迁 `history`） |
| POST | `/bookings/{id}/cancel` | 取消（两小时判定，自动补位；可传 `now`） |
| POST | `/bookings/{id}/drop-waiting` | 候补退出，不计爽约 |
| POST | `/bookings/{id}/no-show` | 登记爽约（自动补位） |
| GET | `/schedule/{date}` | 当晚各时段已确认者与候补队列 |
| POST | `/weather/close-slot` | 单时段天气停开并补位 |
| POST | `/weather/close-night` | 整晚天气停开，不补位 |
| GET | `/events?limit=100` | 审计事件（最新在前） |
| GET | `/health` | 健康检查 |

错误响应统一为 `{"error":{"code":"...","message":"..."}}`，
常见码：`one_slot_per_night`(409)、`member_suspended`(403)、
`not_cancellable/not_waiting/not_confirmed`(409)、`member_not_found/booking_not_found`(404)。

## 预约状态与字段

- 状态：`waiting`（候补中）→ `confirmed`（已确认）→
  `cancelled` / `weather_cancelled` / `no_show`（终态，记录保留）
- `reason`：`early_cancel` / `late_cancel` / `waitlist_drop` / `weather` / `no_show`
- `promoted_from_waitlist=true` 表示该确认是候补补位而来
- `history` 记录每次状态变迁与时间

## 测试

```bash
python3 rules_test.py    # 判定层单元测试（两小时边界等）
python3 smoke_test.py    # 端到端：真实起服 + 重启持久化校验（用独立数据文件）
```

冒烟测试覆盖：同晚唯一时段、候补排队、提前/不足两小时取消、爽约两次自动暂停、
候补遇暂停会员顺延、复核恢复后再补位、按时段/整晚天气停开、候补退出不计次、
以及**停服重启后记录与状态仍在**。
