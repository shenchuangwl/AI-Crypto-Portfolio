# coin-selection (G1–G4 + production state machine)

## Gates

| Gate | Module | Notes |
|---|---|---|
| G1 | `gate1.py` | 6/12/26d avg turnover all > **$3M** |
| G2 | `gate2.py` | 1h ROC/RSI + OHLCV cache for G4 |
| G3 | `gate3.py` | CoinGecko map + supply + **mcap path Mono** |
| G4 | `gate4.py` | §28 weight formula (provisional but formal weights) |
| SM | `state_machine.py` | time-first hysteresis; **no SM_FAST by default** |

## Pseudo-coin overrides

`packages/config/mapping_overrides.json` (copied to `data/coin-selection/coingecko/`):

| Base | coingecko_id | supply (2026-08-12) |
|---|---|---|
| 1000SHIB | `shiba-inu` | OK |
| 1000BONK | `bonk` | OK |
| 1000SATS | `sats-ordinals` | OK (if Gate1 passes) |
| 1000RATS | `rats` | **still supply=0 on CG** → DATA_INSUFFICIENT |

Force remap:

```bash
python3 -m coin_selection --force --force-mapping
```

## Gate3 mcap path

Persists `data/coin-selection/coingecko/mcap_path_YYYY-MM-DD.json` per scan:

- `mcap_calc = circulating_supply × (mark|last / multiplier)`
- Mono_up/down + supply-adjusted log return
- Scores use path when `path_points ≥ 3`, else warm with ret_24h fallback

## Production SM (no SM_FAST)

Default path **clears** leaked `SM_FAST` unless `COIN_SELECTION_ALLOW_SM_FAST=1`.

```bash
# production single scan
python3 -m coin_selection --force --workers 8

# multi-scan natural dwell simulation (15m virtual clock)
python3 -m coin_selection --force --workers 8 --simulate-scans 9

# lab only cascade (not production)
python3 -m coin_selection --force --fast-confirm
```

### 新四区门槛（`param-v1.3.0-dual-path-sticky`）

观察/淘汰**冻结在 v1.2**；符合/确认改为双通道，任一通道命中即可（同时命中记 `PATH_S`）。

```text
cons_is_one(c)      := abs(c - 1.0) <= 1e-9

pass_watch          := Score >= 45                       # 冻结
pass_qualified      := 有供应量 AND (
                         (Score>=58 AND SS>=50 AND M>=55)                    # PATH_S
                      OR (Score>=62 AND SS>=45 AND M>=65 AND C==1.00))       # PATH_M
pass_confirmed      := 有供应量 AND DQ>=60 AND data_mode!=MISSING AND (
                         (Score>=66 AND SS>=50 AND M>=60 AND C>=0.67)        # PATH_S
                      OR (Score>=70 AND SS>=45 AND M>=70 AND C==1.00))       # PATH_M
                       AND NOT (SS<50 AND C<1.00)        # 明令禁止的第三张嘴
hold_ok             := Score>=56 AND (SS>=45 OR (C==1.00 AND M>=62))
```

| 台阶 | 前置 | 驻留 | 连过 | 退出分 |
|---|---|---|---|---|
| 观察 ← NONE/淘汰/数据不足 | — | — | 2 | <40 连 2 → 淘汰 |
| 符合 ← 观察 | 必须已在观察 | 观察 ≥30 min | 2 | <52 连 2 → 退观察 |
| 确认 ← 符合 | 必须已在符合（禁跳级） | 符合 ≥15 min | 2 | `not hold_ok` 连 2 → 退符合 |

确认区的必要条件 N1–N8（全中才看通道）：
`liquidity_hard_pass is True`（**`None` 不算通过**）、有供应量、`DQ≥60`、`data_mode != MISSING`、
当前是符合区、驻留≥15 min、连过≥2 且当轮 `pass_confirmed`、生产未开 `SM_FAST`。

最快确认路径 = 2（进观察）+ 2（进符合，另受观察驻留 30 min 约束）+ 2（进确认，另受符合驻留 15 min 约束）
= **6 个 15 分钟节点 / 首次出现后 75 分钟**（实测，`Score=SS=M=100` 也不能更快；
对照现行 v1.2 的 2+3+4=9 节点 ≈135 分钟）。禁止跳级，每轮最多升一级。

即时退化：缺供应量 / `DQ<60` / `data_mode==MISSING` → 立刻退回符合；门槛一失败 → 立刻淘汰。

### 时间栅格（15m 节点，唯一对外口径）

`scan_id` 一直是量化的（`seq = ⌊(now−anchor)/900⌋`），但时间戳曾经用墙钟。任何
**非整点开跑**的扫描（`--force`、重启、慢唤醒）会把 17:52:14 盖进 node 071
（真实节点 17:45:00），此后该行的停留时长永久带着这段漂移——这就是 `38m` /
`1h2m` / `1-4 分钟` 异常的来源。

现在：

| 字段 | 时钟 |
|---|---|
| `meta.scan_timestamp_utc`、`state_enter_time_utc`、`expires_at_utc`、DMR `scan_timestamp_utc` | **节点时间** `anchor + seq×900` |
| `generated_at_utc` | **墙钟**（保留"实际何时产出"的可审计性） |

- `node_time(anchor, seq)`：节点时间。
- `floor_to_node(ts)`：绝对秒向下取整到 900 栅格。`86400 % 900 == 0` 且 Unix 纪元起于 UTC 午夜，
  所以它与 `anchor + seq×900` 恒等，无需按日换算。
- `_enter_state` 落 `floor_to_node(now)`；`dwell_minutes` 两端都取节点 →
  **停留时长恒为 15 的整数倍**，`dwell >= 30` 这类边界闸也不会再出现 29.98 的擦边失败。
- `quantize_enter_times(store)`：一次性迁移，把历史 off-grid 的 `state_enter_ts` 拉回栅格，
  每轮扫描开头自动执行且幂等。

## CoinGecko API key

Put a **Demo** key in the gitignored monorepo `.env` (never commit it):

```bash
cp .env.example .env
# COINGECKO_API_KEY=CG-...
# COINGECKO_USE_PRO=0
```

- Demo key → `api.coingecko.com` + `x-cg-demo-api-key` (default)
- Pro key  → set `COINGECKO_USE_PRO=1` → `pro-api.coingecko.com` + `x-cg-pro-api-key`

`scripts/daemonize.py` injects `.env` into the 15m loop. Gate3 then:

1. maps Binance USDT-M → CoinGecko id (plus `1000SHIB/BONK/SATS/RATS` overrides)
2. batches `/coins/markets` for `circulating_supply` / CG `market_cap`
3. computes **流动市值** `market_cap_calculated = circulating_supply × (mark|last / multiplier)`
4. writes the 15m mcap path used by S_MC; `supply_missing` blocks QUALIFIED/CONFIRMED

Without a key: demo host, gentler batching, may hit 429.

## Outputs

- `data/coin-selection/latest.json` → api-gateway
- `data/dmr-adapter/inbox/{scan_id}.candidates.json` — **CONFIRMED only**
- `data/coin-selection/state_machine.json` — persistent SM
