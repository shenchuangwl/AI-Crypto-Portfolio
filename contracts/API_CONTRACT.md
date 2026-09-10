# 接口契约落地骨架 v1

对齐：选币 v1.2 §37 / §39 · OpenAPI · JSON Schema · TS · Pydantic

---

## 1. REST 一览（api-gateway `/api/v1`）

| 方法 | 路径 | 用途 | 响应 schema |
|---|---|---|---|
| GET | `/boards` | 板面变体注册表（选币榜 / 选币榜Y） | `{ count, boards[] }` |
| GET | `/screener/latest` | 最新 15m 榜单快照 | `screener-snapshot.schema.json` |
| GET | `/screener/scans/{scan_id}` | 历史不可变快照 | 同上 |
| GET | `/screener/symbols/{symbol}` | 单币详情 + 当日 path≤96 | `SymbolScreenerDetail` |
| GET | `/screener/confirmed` | 确认区 + 双口径 + control/dmr/alerts | `{ count, long[], short[], occupancy, daily_unique, control, dmr, alerts }` |
| GET | `/screener/ready-confirm` | 符合区待确认副出（`consumable_by_dmr=false`） | `{ count, rows[] }` |
| GET | `/market/{symbol}/klines` | OHLCV | `{ symbol, interval, bars[] }` |
| GET | `/dmr/candidates?scan_id=` | 执行层回拉 | `dmr-candidate-message[]` |
| GET | `/review/coverage` | 复盘账本覆盖窗口 + 字段可用起点 + 版本分段 + 新鲜度 | `{ first_ts, last_ts, available_days, zone_available, field_availability, parameter_segments, freshness }` |
| GET | `/review/summary` | 复盘汇总指标（§15 全套） | `{ coverage, summary }` |
| GET | `/review/trades` | 复盘交易分页 | `{ coverage, summary, trades[], count, total, truncated }` |
| GET | `/review/symbols/{symbol}` | 单币复盘逐条 | 同上（`symbols` 被钉死为该币） |

OpenAPI：`contracts/openapi/screener-api.v1.yaml`

### 1.0 板面变体（`/screener` / `/screener-x` / `/screener-y`）

`/screener/*` 下的**全部 5 个只读接口**（`latest` / `scans/{id}` / `symbols/{sym}` /
`confirmed` / `ready-confirm`）与 SSE `events`，在 `/screener-x/*` 与 `/screener-y/*` 下同形存在。
三者由 `packages/config/board-variants.json` 注册，网关只做前缀 → 数据目录的映射，
没有第二套 handler：

| | `/api/v1/screener` | `/api/v1/screener-x` | `/api/v1/screener-y` |
|---|---|---|---|
| 栏目 | 选币榜 | 选币榜X | 选币榜Y |
| `meta.parameter_version` | `param-v1.4.0-staircase-confirm-dmr` | `param-v1.3.0-screener-x` | `param-v2.0.0-screener-y` |
| `meta.board_key` | 缺省视为 `main` | `x` | `y` |
| `meta.cycle` | 不存在，无重置 | `{enabled:false}`，无重置 | 24h / 00:00 UTC |
| 数据目录 | `data/coin-selection` | `data/coin-selection-x` | `data/coin-selection-y` |
| DMR inbox | `data/dmr-adapter/inbox` | `data/dmr-adapter-x/inbox` | `data/dmr-adapter-y/inbox` |
| `confirmed.consumable_by_dmr` | `true` | `false` | `false` |

X/Y 均复用主扫描 G1–G4 原始指标，不新增交易所/CoinGecko 请求。
选币榜X 对外 v1.3.0 当前策略复刻 main v1.4.0，**不是旧 dual-path-sticky**。
`overrides.settings={"mcap_zone_mode":"shadow"}` / `state_config={}`：四列只观察，
不改变状态/排序/DMR；池行 `effective_zone` 报真实 state，避免假设区误入复盘。
X 的快照与独立 `review/ledger.sqlite` 使用同一个 `param_hash`，其身份不等同 main。
后续唯一机器入口为 `boards[key=x].overrides`，修改时同步 X YAML 与新 rule revision。
Y 现网是七权重 + 主导层 on + 周期重置，不再是空 overrides 的复刻。

#### 1.0.1 `meta.cycle` —— 时间区（24 小时循环）

主榜快照没有 `meta.cycle`；X 投影显式写 `{enabled:false}`，不得仅按键存在判断启用；
「选币榜Y」是 24h 循环，周期起点就是既有的每日锚点 00:00 UTC（`scan_id` 的 000 号节点）。

```json
{
  "enabled": true, "period_hours": 24, "anchor_utc": "00:00",
  "cycle_key": "20260825T0000Z",
  "cycle_start_utc": "2026-08-25T00:00:00Z", "cycle_end_utc": "2026-08-26T00:00:00Z",
  "node_in_cycle": 19, "nodes_per_cycle": 96,
  "reset_at_this_node": false,
  "last_reset_cycle_key": "20260825T0000Z", "last_reset_scan_id": "20260825-000",
  "last_reset_at_utc": "2026-08-25T00:00:00Z", "last_reset_node_in_cycle": 0,
  "partial_cycle": false, "partial_reason": null,
  "warmup": { "enabled": false, "nodes": 0, "active": false, "state_config": {} }
}
```

| 字段 | 含义 |
|---|---|
| `cycle_key` | 周期身份，也是重置的**幂等键**：同一周期内重复扫描只清一次 |
| `node_in_cycle` / `nodes_per_cycle` | 本周期第几个 15m 节点 / 一个周期共几个节点 |
| `reset_at_this_node` | 本节点清空了全部分区并从零重建。该节点的确认区必然为空 |
| `closed_zones` | 只在重置节点出现：清空前一刻各分区的成员数 |
| `partial_cycle` | 本周期的分区不是从 0 号节点开始重建的（`cold_start` 首次启用 / `missed_cycle_node` 循环停机跨过了节点）。该周期的统计口径不足一个完整周期 |
| `warmup.active` | 重建加速窗口内（默认永远 false） |

重置后状态机从 NONE 起步，仍受 v1.4.0 时间门槛约束：观察区第 1 节点、符合区第 3 节点、
**确认区 / DMR 区第 5 节点（01:15 UTC）**。每个周期头 75 分钟确认区为空是设计，不是缺数据；
行情、涨跌幅、周期市值等级列当刻就是全的。消费方不要把空确认区当成故障重试。

次级板面尚未产出时返回 **503** `board_snapshot_missing`（**不回落 example** ——
拿 v1.4.0 的示例板面冒充 v2.0.0 的实时结果比空着更糟）。未知前缀仍走既有 404。

`GET /boards` 每项含 `key / label / parameter_version / api_prefix / web_route /
data_dir / dmr_inbox / primary / projected / dmr_executable / enabled /
settings_overrides / state_config_overrides / latest_exists / ledger_exists`。

### 1.1 复盘查询参数（`/review/*`）

| 参数 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `board` | `main｜x｜y`（别名 `ruleset`） | `main` | 选择独立账本：main=v1.4.0，x=v1.3.0 选币榜X，y=v2.0.0。未知值静默回 main；X 不是主榜旧 v1.3.0 历史段 |
| `zones` | CSV of ReviewZone | `DMR,CONFIRMED` | 覆盖 `preset` |
| `preset` | `executable｜funnel｜control` | `executable` | 一键分区组合 |
| `direction` | `up｜down｜both` | `both` | 候选池 |
| `symbols` | CSV，`canonical_asset_id` 或 `symbol` | 空=全量 | 大小写不敏感 |
| `from` / `to` | UTC ISO8601 | `to` = 账本水位（**不是墙钟**） | 无时区值按 UTC 解释并归一为 `…Z`；非法值被丢弃并回显在 `filters.invalid_params` |
| `attribution` | `exit｜enter｜contained` | `exit` | 见下 |
| `include_open` | bool | `0` | 仅影响**行列表**；汇总恒为 CLOSED（R1） |
| `only_flagged` | bool | `0` | 只看带 `flags` 的异常记录 |
| `cycles` | int 1–366 | 空 | **周期对齐窗口**：最近 N 个周期。只对有时间区的板面（`board=y`）有效，传了它就由服务端按 `CycleConfig` 重算 `from`/`to` 并覆盖之。对 `board=main` 是空操作 |
| `whole_cycles` | bool | `0` | 与 `cycles` 连用：只算已经跑完的周期，排除进行中的当前周期 |
| `exclude_flags` | CSV of ReviewFlag | 空 | 剔除带这些旗标的交易。主用途 `exclude_flags=CYCLE_RESET`：把选币榜Y 24h 周期重置的强制平仓拿掉，只看策略自己的退出决策。对 `board=main` 是空操作（它一条 `CYCLE_RESET` 都没有） |
| `pv` | CSV，参数版本 | 空/`all`=全部 | 只统计该套选币标准产出的交易（按**入点**版本）。跨版本汇总无意义，见下 |
| `min_dwell_nodes` | int | `0` | 同时作用于行、汇总与未平仓计数 |
| `sort` / `desc` | 白名单键 / bool | `enter_time_utc` / `0` | **服务端排序**，作用于整个窗口而非当前页 |
| `limit` / `offset` | int | `500` / `0` | 上限 5000；响应含 `total` 与 `truncated` |

`sort` 白名单：`enter_time_utc, exit_time_utc, dwell_minutes, dwell_nodes, pnl_pct, pnl_sign, score, direction_confidence, symbol, zone, enter_price, exit_price`。白名单外的值回落到默认，绝不进 SQL。

**周期归属口径**：`exit` = 这段时间**实现**了多少盈亏（默认，对账单口径）；`enter` = 这段时间**选出**的币后来怎样；`contained` = 两端都在窗口内（**仅 CLOSED**，与 `include_open` 无关）。切换口径笔数会变属正常——一笔只归属一个周期，不拆仓。

**周期重置与复盘**：启用了时间区的板面（选币榜Y）在每个周期节点会把全部分区清空，
账本因此会在该节点产生一批**强制平仓**。这些交易带 `CYCLE_RESET` 旗标，
与策略自己退出的交易区分开——不区分的话，每天上千笔强平会把 v2.0.0 的胜率彻底污染。
`meta.cycle` 只有启用周期的板面才有，所以 `board=main` 的账本里
（历史的与将来的）一条 `CYCLE_RESET` 都不会出现。

重置节点上币会整个从板面消失（板面只登载分区成员），所以这些平仓的
`exit_price_source` 是 `prev_node`（上一节点的最后一次打印），
且 `CYCLE_RESET` **取代** `VANISHED` —— 原因已知，不是数据异常。
用 `exclude_flags=CYCLE_RESET` 可以把它们整段剔除；
`summary.flag_counts.CYCLE_RESET` 随时给出本窗口内的强平笔数。

实测（2026-08-25，可执行区近 7 天）：含强平 1580 笔 / 胜率 0.3899，
排除强平 1367 笔 / 胜率 0.3650 —— 差 2.5 个百分点，不区分就会把结论读反。

**窗口必须落在周期栅格上**：选币榜Y 每 24h（00:00 UTC）清空全部分区重建，
所以「水位往前推 N×24h」的滚动窗口会从半夜切开两个周期 —— 既漏掉当前周期的头几个小时，
又把上一个周期的尾巴连同它的重置强平算进来。实测同一时刻两种口径的均值连正负号都不一样。

边界约定（利用「重置后没有已平仓交易能跨周期」这一事实，
周期 C 的交易满足 `enter ∈ [C_start, C_end)` 且 `exit ∈ (C_start, C_end]`）：

| 归属口径 | 窗口闭合 | 00:00:00 的重置强平归属 |
|---|---|---|
| `exit` | `(start, end]` | 上一个周期（它是那个周期的收尾） |
| `enter` | `[start, end)` | —— |
| `contained` | `[start, end]` | —— |

三种口径下逐周期笔数完全相同（相邻周期既不重叠也不漏），有验证闸钉住。
响应的 `filters.cycle_window` 回显服务端最终采用的窗口，含
`first_cycle_key` / `last_cycle_key` / `includes_partial_current` / `empty_current_cycle`。
`coverage.cycle` 给出栅格本身（当前周期、进度、账本内完整周期数）。

**`board` 与 `pv` 是两层，务必区分**：`board` 换的是**整本账本**（哪一套选币机制
产出的交易，两块板面各有一个 sqlite 文件，**从不合表**）；`pv` 是在**同一本账本内**
只看某一段参数版本的成绩。响应回显 `board` 与 `board_parameter_version`。
`board=y` 且该账本未铺底时返回 **503** `ledger_not_ready`，`hint` 指向
`scripts/replay_screener_y.py`（而不是主板面的 `build_review_ledger.py`）。

**参数版本口径**：交易记的是**入点**的 `parameter_version` —— 那是它被选中时所依据的标准。`pv=` 过滤等于「只看这一套标准自己的成绩」。选入门槛变过，跨版本混算胜率没有意义（§14.4），所以栏目顶部常驻一条版本切换条。

**计数口径（与 `/screener` 相反，务必区分）**：`trades` = 完整「选入+退出」笔数，**不按币去重**；多分区取 `zones` 时笔数**相加**（`/screener` 的多选是并集去重）。`unique_coins` 仍跨区去重。`open_trades` 永不进任何盈亏分母。

账本未建时返回 **503** `ledger_not_ready`；查询异常返回 **500** `review_query_failed`（不会拖垮网关的选币接口）。前端禁止自行扫 snapshots 算盈亏。Schema：`review-trade.schema.json`。

### 1.2 复盘账本新鲜度

`coverage.freshness` 比较账本水位与**该板面自己**的 `latest.json` 的当前扫描节点
（`main` → `data/coin-selection/latest.json`，`y` → `data/coin-selection-y/latest.json`）：

```json
{ "stale": true, "latest_scan_id": "20260824-024", "lag_nodes": 3, "lag_minutes": 45.0, "hint": "…" }
```

`stale=true` 时前端必须显示蓝色横幅并说明「下方数字截至账本节点」。账本由 `scan.py` 落盘后的旁路钩子增量写入，钩子会自动**补齐**水位到目标节点之间被跳过的所有节点；缺口超过 `MAX_CATCHUP_NODES`（240 个节点 ≈ 2.5 天）时拒绝写入并保持 `stale`，由 `scripts/build_review_ledger.py` 重建。

---

## 2. `GET /screener/latest` 语义

### 2.1 行为

1. 返回**最近一次成功完成**的扫描快照（含 long/short 池）。
2. 客户端默认展示 `state ∈ {CONFIRMED, QUALIFIED}`；`WATCH` / 隔离态由 UI 过滤或 `include_isolated=true`。
3. `expires_at_utc` 通常 = `scan_timestamp_utc + 15m`。过期后 UI 显示 stale 横幅，**不清空表格**。
4. 收到 WS `screener.updated` 后 refetch；禁止每秒轮询全表。
5. **禁止**用 ticker 的 `last_price` 回写 `score_*` / `state`。

### 2.2 字段映射（§37 模板 → JSON）

| 榜单列 | CandidateRow 字段 |
|---|---|
| 合约 | `symbol` |
| 状态 | `state`（`WATCH`=观察） |
| 入选时间 | `state_enter_time_utc`（进入**当前**状态的本地时钟，展示 `8.17 14:15`） |
| 停留时间 | `state_duration_minutes`（当前状态已持续多久） |
| 停留价格 | `state_enter_price`（进入当前观察/符合/确认时的 last/mark，固定不跟盘；淘汰等为 null） |
| 盈亏百分比 | 前端现算：上涨 `(Last*/停留价格 − 1)`，下跌 `(1 − Last*/停留价格)`；缺停留价为 — |
| 盈亏数值 | 由盈亏百分比映射：`>0 → 1`，`=0 → 0`，`<0 → −1` |
| Score↑ / Score↓ | `score_up` / `score_down` |
| DirConf | `direction_confidence` |
| $S_L$ / 等级 | `liquidity_score` / `liquidity_grade` |
| 30m流通市值 / 2h流通市值 / 6h流通市值 | `mcap_grade_30m` / `mcap_grade_2h` / `mcap_grade_6h`（A–F，均线明细见 `mcap_tf.{30m,2h,6h}`；判不出级为 `null`，展示 `—`） |
| 1h / 4h / 24h | `ret_1h` / `ret_4h` / `ret_24h`（滚动窗口涨跌幅，小数） |
| 1Week / 1Month | `ret_1w` / `ret_1mo`（近 7 天 / 近 30 天涨跌幅；日 K 根数不足为 `null`，展示 `—`） |
| 锚点以来 | `ret_since_anchor` |
| $M$ / $S_{MC}$ / $SS$ | `momentum_score` / `mcap_momentum_score` / `staircase_score` |
| $C$ | `consistency_score`（0–1 或 0–100，见 adapter） |
| AQV 6/12/26 | `aqv_*_m`（百万 USD） |
| $M^{CG}$ / $M^{Calc}$ | `market_cap_coingecko` / `market_cap_calculated` |
| data_mode / 风险 | `data_mode` / `risk_flags` |

| 通道 | `qualified_path` / `confirmed_path`（`S`/`M`，审计与展示，不参与判定） |
| 未确认原因 | `not_confirmed_reasons`（`liquidity`/`supply_missing`/`dq`/`ss`/`momentum`/`consistency`/`score`/`data_mode`/`dwell`/`streak`） |
| 待确认标签 | `ready_confirm`（符合区已满足 `pass_confirmed`，只欠驻留或连过；**不是**可下单信号） |

Header 条 → `meta.*`（anchor、scan_id、regime、credits、state_counts）。

#### 2.2.1 周期流通市值等级 `mcap_grade_*`（选币榜「等级」与「1h」之间的三列）

列顺序固定为 `30m流通市值` → `2h流通市值` → `6h流通市值`，
DMR / 确认 / 符合 / 观察 / 淘汰 / 数据不足 / 低置信度 七个分区共用同一张状态栏表格，列结构一致。

```
流通市值_i     = 流通供应量 × (第 i 根当前周期 K 线收盘价 / 合约乘数)
6 日平均流通市值  = 最近 6  根当前周期 K 线的流通市值总和 / 6
12 日平均流通市值 = 最近 12 根当前周期 K 线的流通市值总和 / 12
26 日平均流通市值 = 最近 26 根当前周期 K 线的流通市值总和 / 26
```

「日」沿用既有命名，实际含义是**当前周期下的 K 线根数**。三个周期使用**完全相同**的一套等级规则：

```
A：6 日平均流通市值 > 12 日平均流通市值 > 26 日平均流通市值 | 多头排列
B：12 日平均流通市值 > 6 日平均流通市值 > 26 日平均流通市值 | 多头轻度回调
C：12 日平均流通市值 > 26 日平均流通市值 > 6 日平均流通市值 | 多头重度回调
D：12 日平均流通市值 < 26 日平均流通市值 < 6 日平均流通市值 | 空头重度回调
E：12 日平均流通市值 < 6 日平均流通市值 < 26 日平均流通市值 | 空头轻度回调
F：6 日平均流通市值 < 12 日平均流通市值 < 26 日平均流通市值 | 空头排列
```

#### 2.2.2 周期涨跌幅 `ret_1w` / `ret_1mo`（选币榜「24h」与「锚点以来」之间的两列）

列顺序固定为 `… → 24h → 1Week → 1Month → 锚点以来 → …`，
DMR / 确认 / 符合 / 观察 / 淘汰 / 数据不足 / 低置信度 七个分区共用同一张状态栏表格，
上涨候选池与下跌候选池共用同一份涨跌幅（不按方向改写），列结构一致。

口径**严格参照**既有的 `1h` / `4h` / `24h` 三列，只是取数周期不同：

```
涨跌幅  = 最新收盘价 / 回看 N 根之前的收盘价 − 1

1h     : 1h K 线回看 1  根
4h     : 1h K 线回看 4  根
24h    : 1h K 线回看 24 根
1Week  : 1d K 线回看 7  根
1Month : 1d K 线回看 30 根
```

两列同样是**滚动窗口**收益率（近 7 天 / 近 30 天），不是「本周 K 线开→收」或
「本自然月开→收」—— 与 `24h` 表示"最近 24 小时"而不是"今天"完全一致。
与 1h 序列一样**保留正在形成的最后一根日 K**：它的收盘价就是当前最新价。

事实源 `services/coin-selection/src/coin_selection/long_returns.py`。
日 K 根数不足以回看时为 `null`（前端 `—`），**绝不折叠成 `0`**——`0` 会被读成
"这周没涨没跌"。两个键在每一行上永远存在，列结构因此在七个分区里完全一致。
**纯展示列**：不进 Score / 状态机 / DMR。

硬约束：等级定义不得修改；不得用其它名称替代 `A/B/C/D/E/F`；
**上涨候选与下跌候选共用同一套等级**（`long_pool` 与 `short_pool` 同一 symbol 的三列必然相同）；
K 线不足 26 根或三条均线并列时字段为 `null`（前端 `—`），**不得**新造第七个等级名。
这三列是展示列：不参与 Score、状态机、DMR 精选与任何门槛判定。

`mcap_tf.<tf>.ma6/ma12/ma26` 是均线的绝对 USD 值；流通供应量未知时为 `null`
（`supply_known=false`），但 `grade` 依然有效 —— `流通供应量 / 合约乘数` 是正的公共因子，
不改变三条均线的大小关系。运行统计见 `meta.mcap_tf`。

三列均**可点表头排序**（纯前端，不回请求、不改快照）。排序秩直接沿用 A→F 这条
「流通市值增长最强 → 缩减最强」的单调阶梯：`A=6 B=5 C=4 D=3 E=2 F=1`，
`null`（判不出级）记 `0`，因此降序为 `A→F→—`、升序为 `—→F→A`。
同级行回落到后端下发的 `rank`（Score 降序）—— 排序是稳定排序，不引入第二排序键。

### 2.3 三个数量口径（必须同框展示）

| 口径 | 字段 | 含义 |
|---|---|---|
| 瞬时占用 | `meta.occupancy.confirmed_unique` / `qualified_unique` | 本轮该区去重币数，DMR 当刻可消费集合，目标带 10–20 |
| 日去重入选 | `meta.daily_unique.{confirmed,qualified,watch,eliminated}` | 本锚点日至少进过一次该区的去重币数，只增，跨锚点重置 |
| 侧 | `meta.state_counts.*` | `(symbol, direction)` 行数，页头「观察/符合/确认 N」即此 |

任何只给其中一个口径的展示都是错的：日去重会被读成「现在能同时交易这么多」。

### 2.4 动态控制（`meta.control` / `meta.alerts`）

`n_impulse`（G1 侧中 `M≥70 且 C=1.00` 的数量）、`live_ratio`（G1 去重币里 `data_mode==LIVE` 的占比）、
`universe_dev`、`freeze_new_confirm`、`low_breadth`、`data_stress`、`universe_stress`。

`alerts ⊆ {LOW_BREADTH, CONFIRM_UNDERFILLED, CONFIRM_OVERFLOW, DATA_STRESS, UNIVERSE_STRESS}`
**只标注不放宽**：不降 N1–N4、不撤 F2、不动 300 万美元硬门槛、不把符合区放给 DMR。
`freeze_new_confirm=true` 时冻结**新晋**确认，已确认改走 `hold_ok`。

### 2.5 `GET /screener/ready-confirm`

副出符合区里已满足 `pass_confirmed`、仅欠驻留/连过的行。响应恒带
`consumable_by_dmr: false` —— 它是审计视图，不是第二个可下单区。

### 2.6 样例

见 `contracts/examples/screener-latest.example.json`。

---

## 3. DMR JSON（§39）

### 3.1 传输

| 通道 | 用途 |
|---|---|
| PostgreSQL 表 + `NOTIFY` | 主路径 |
| Redis Stream `dmr:candidates` | 备路径 |
| `GET /dmr/candidates?scan_id=` | 执行器回拉 |

### 3.2 主键与幂等

- 业务主键：`(anchor_date, scan_id, symbol, direction)`
- `message_id = sha1(anchor_date|scan_id|symbol|direction)`
- 同一 `(anchor_date, scan_id, symbol)` **仅一条方向**（up→`LONG`，down→`SHORT`）

### 3.3 拒绝消费（§39.3，已实现于 TS/Python helper）

```
state != CONFIRMED（若只吃确认区）
direction == NEUTRAL
now > expires_at_utc
data_mode == MISSING 或 data_confidence < 60
risk_flags 含 H*
circulating_supply null 或 ≤ 0
parameter_version 不在白名单
total_score < 执行器阈值
```

白名单默认 `["param-v1.3.0-dual-path-sticky", "param-v1.2.0-g1g2g3g4-path"]`，
由 `DMR_PARAM_WHITELIST`（逗号分隔）覆盖。**换参数标签必须同时更新白名单**，
否则整批消息会以 `parameter_version` 被拒（这正是该条的目的：防静默参数漂移）。

`coin-selection` 侧还会在写 inbox 前做两件事，执行器不必重复：
同一 `symbol` 只留 `total_score` 更高的一侧（v1.2 §35.3），
再按 `total_score desc → staircase desc → momentum desc → symbol asc` 取 **Top-K 去重币**
（`K` 默认 16，合法区间 12–20；被截断的消息带 `dmr_truncated=true` 留在 `all_confirmed` 里）。
`confirm_path`（`S`/`M`）随消息下发，供执行器按通道区分仓位/止损，不影响是否消费。

### 3.4 与榜单行映射

| CandidateRow | DmrCandidateMessage |
|---|---|
| direction up/down | LONG / SHORT |
| score_up 或 score_down | `total_score`（取 active 方向） |
| consistency_score | `trend_consistency_score`（若 ≤1 则 ×100） |
| mcap_momentum_score | `market_cap_momentum_score` |
| 其余同名或近义字段 | 见 `candidate_row_to_dmr_message` |

样例：`contracts/examples/dmr-candidate.example.json`  
Schema：`contracts/json-schema/dmr-candidate-message.schema.json`

---

## 4. WebSocket（ws-gateway）

| 频道 | Payload 要点 |
|---|---|
| `screener.updated` | `{ scan_id, anchor_date, scan_timestamp_utc, expires_at_utc }` → 客户端 REST refetch |
| `ticker:{symbol}` | last/mark/ts；**只更新价格列** |
| `kline:{symbol}:{interval}` | 增量 K |
| `book:{symbol}` | 盘口 |
| `trades:{symbol}` | 成交 |

**禁止**每 15 分钟 WS 推送整表 500+ 行。

---

## 5. 代码落点

| 语言 | 路径 |
|---|---|
| TypeScript | `contracts/typescript/screener.ts` |
| Python | `contracts/python/models.py` |
| OpenAPI | `contracts/openapi/screener-api.v1.yaml` |

前端 F1（`/data120/screener-web`）迁移时：

1. 将 `OBSERVE` → `WATCH`（或边界映射）
2. `fetchScreenerLatest` 改打真实 `GET /api/v1/screener/latest`
3. 共享类型改为引用 `contracts/typescript`（workspace package）

---

## 6. 校验命令（本地）

```bash
# JSON Schema（需 jsonschema）
python3 - <<'PY'
import json
from pathlib import Path
try:
    import jsonschema
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "jsonschema", "-q"])
    import jsonschema

root = Path("contracts")
for schema_name, example_name in [
    ("screener-snapshot.schema.json", "screener-latest.example.json"),
    ("dmr-candidate-message.schema.json", "dmr-candidate.example.json"),
]:
    schema = json.loads((root / "json-schema" / schema_name).read_text())
    example = json.loads((root / "examples" / example_name).read_text())
    jsonschema.validate(example, schema)
    print("OK", example_name)
PY

# Pydantic
python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, "contracts/python")
from models import ScreenerSnapshot, DmrCandidateMessage, dmr_should_reject
snap = ScreenerSnapshot.model_validate_json(Path("contracts/examples/screener-latest.example.json").read_text())
msg = DmrCandidateMessage.model_validate_json(Path("contracts/examples/dmr-candidate.example.json").read_text())
print(snap.meta.scan_id, len(snap.long_pool), dmr_should_reject(msg))
PY
```
