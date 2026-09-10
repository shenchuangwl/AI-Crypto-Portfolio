# Contracts — 真相源

对齐：选币 v1.2 §37 / §39 · 前端设计 v1.0 · monorepo 拆分 v1

| 路径 | 用途 |
|---|---|
| `openapi/screener-api.v1.yaml` | REST OpenAPI 3.0 |
| `json-schema/screener-snapshot.schema.json` | `/screener/latest` 响应 |
| `json-schema/dmr-candidate-message.schema.json` | §39 DMR 消息 |
| `typescript/screener.ts` | 前端/网关 TS 类型 |
| `python/models.py` | 后端 Pydantic v2 |
| `examples/*.json` | 可校验样例 |

## 生成约定（后续）

```bash
# 可选：用 openapi-generator / datamodel-code-generator 从 schema 生成
# 当前为手写对齐骨架，保证与 F1 类型兼容并修正 WATCH 命名
```

## 状态枚举（API 规范）

- 状态机：`NONE | WATCH | QUALIFIED | CONFIRMED | ELIMINATED | DATA_INSUFFICIENT | LOW_CONFIDENCE`
- 前端展示「观察」← `WATCH`（F1 旧名 `OBSERVE` 仅兼容别名，新代码用 `WATCH`）

## 硬规则

1. 榜单字段只读；`last_price` 不得回写 `score_*`
2. 同一 `(anchor_date, scan_id, symbol)` 仅一条 `direction`
3. DMR 消息 `expires_at_utc = scan_timestamp + 15min`
4. `attribution` 透传 CoinGecko 署名
