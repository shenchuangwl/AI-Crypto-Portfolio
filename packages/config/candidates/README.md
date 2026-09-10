# 候选配置目录（**生产永不自动加载**）

放在这里的文件是**尚未获得切换授权**的候选件。它们不会被任何生产代码路径读取。

## 为什么必须另起一个目录

`coin_selection.rule_manifest.list_published()` 对 `packages/config/rule-manifests/`
做 `glob("*.json")`，`effective_manifest()` 的判据只有一条：

```
effective_from_scan_id <= scan_id  ⇒  生效
```

也就是说，**把一份格式合法的 manifest 放进 rule-manifests/ 就等于让它到点自动上线**，
不需要任何人再点一次头。提示词第十一节明确要求「不提前创建可自动生效 manifest」，
所以候选件只能放在这个不被 glob 的目录里。

机器保证见 `services/coin-selection/tests/test_x_v13_candidate_config.py`。

## 当前内容

| 文件 | 用途 | 状态 |
|---|---|---|
| `x-v1.3.0-r2.candidate.json` | 选币榜X 从「v1.4 克隆」切到「历史 v1.3 四区 + 仅作用于 DMR 的 216 约束」后的规则身份 | **待授权**，未发布 |
| `board-variants.x-adapted.json` | 切换时要写进 `board-variants.json` 的 `boards[key=x].overrides` 片段 | **待授权**，未生效 |

---

# 选币榜X v1.3 适配 · 生产切换单

> 本单据是**申请**，不是执行记录。未获得明确授权前，下面一条都不执行。

## 1. 最终策略语义

- **四区**：历史 v1.3 双通道 / 粘滞
  - 确认 PATH_M 恢复：`Score>=70 ∧ SS>=45 ∧ M>=70 ∧ C==1`（S 优先）
  - 确认 hold 改析取：`Score>=56 ∧ (SS>=45 ∨ (C==1 ∧ M>=62))`
  - 取消 v1.4 的「CONFIRMED 且 SS<50 立即降级」全局硬楼梯分支
  - 观察 / 符合两档**不动**（v1.3 与 v1.4 本就相同）
- **DMR**：`D(t) = TopK( C(t) ∩ B(t) ∩ A216(t) )`
  - `C(t)` = v1.3 状态机在该节点输出的 CONFIRMED 集合
  - `B(t)` = 基础消息数据条件（有供应 ∧ `liquidity_hard_pass is True` ∧ `DQ>=60` ∧ `data_mode != MISSING`）
  - `A216(t)` = 该方向 ceiling == DMR 的组合（每侧 12/216）
  - 不再套用 v1.4 的 `70/55/65/.67/70/LIVE` 强子集
- **216 只约束 DMR**：`mcap_zone_mode` 保持 `shadow`，四区归属 / 排序 / 入区时刻零触碰
- X 继续**不可执行**

## 2. 新身份

| 项 | 值 |
|---|---|
| rule_revision | `x-v1.3.0-r2`（不覆盖已发布的 `r1`） |
| param_hash | `pf1_cc6b176b336be60b`（克隆期为 `pf1_b2d6cd1ec88bf4af`） |
| config_hash | `sha256:6ec0fa8a04c9814c9f4121775b454caedc8bcd58e9e9965756bc606b7b28a71f` |
| mcap_mapping_version / mapping_hash | `mcap-216-v2.0.0-r1` / `sha256:da14429d…`（不变） |
| 生效节点 | **待定** —— 必须是 00:00 UTC 周期边界，切换时填 |

## 3. 数据分段

- 旧克隆段（`pf1_b2d6cd1ec88bf4af`）账本**原样保留**，不重写、不回填、不与新段合并统计。
- 新段从获批的 UTC 边界起，以新 `param_hash` + `x-v1.3.0-r2` 自然分段。
- OPEN 成员：切换时仍在持仓中的边，**继续由旧段规则管理直到自然平仓**；
  不伪造市场平仓，不抹掉旧入场价。跨段的边在复盘里以入点 `param_hash` 归属。

## 4. 回归状态（本轮实跑，证据见 `doc/verification/screener-x-v13-continue-20260906/`）

- `scripts/run-tests.sh`：45 PASS / 0 FAIL
- `check_main_frozen.py`：0 hard failures（克隆形态与候选形态均通过；只开一半判红）
- main / Y 的 `param_hash` 与 `config_hash` 在四种 X 配置下逐字节不变
- 隔离回放 326 节点：四区/排序/入区时刻差异 **0**；DMR ⊆ CONFIRMED；216 排除 43 条确认边
- `verify_rule_consistency.py --board x`（候选语义）：K0–K9 全 PASS，在线↔离线差异 **0**

## 5. 已知风险 / 未解除阻塞

1. **G4 输入窗口仍是 168 根**（历史 v1.3 为 48 根）。`prepare_v13_rows` 尚未接线，
   现有缓存不含时间戳因而物理上无法满足其 PIT 契约。
   ⇒ 本次切换交付的是「v1.3 阈值 + 现代 168 根输入」，**不是**逐字复原当年语义。
   这一差异必须同时写进 manifest notes、板面 meta 与前端说明，否则属于身份说谎。
2. **Top-K 成为实际裁决者**：216 合格集在真实节点上为 20–38，大于 `dmr_top_k=16`，
   最后一刀由 Score/SS/M 排序截断决定。切换前需产品侧确认这是否是期望行为。
3. **C12 历史反例仍在**：`TRIAUSDT|down|DMR|20260902-033` 主榜账本退出价 `0.003729`
   与快照 `0.003752` 不符。属开工前既有问题，本轮不授权修改主榜历史。

## 6. 回滚方法

把 `board-variants.json` 的 `boards[key=x].overrides` 改回：

```json
{"settings": {"mcap_zone_mode": "shadow"}, "state_config": {}}
```

并停用 `x-v1.3.0-r2`（从 `rule-manifests/` 移回本目录）。身份随之回到
`pf1_b2d6cd1ec88bf4af` / `x-v1.3.0-r1`，新段数据保留但不再增长。
**不删除**任何已写入的账本行。
