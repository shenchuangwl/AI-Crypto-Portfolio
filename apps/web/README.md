# apps/web — AI Crypto Portfolio 前端终端

React 19 + Vite 8 + TypeScript 单页应用。生产环境由 `services/api-gateway` 托管 `dist/`（默认 `http://127.0.0.1:18080`），
开发时由 Vite 提供热更新并把 `/api` 反向代理到网关。完整说明见仓库根目录 [README](../../README.md)。

## 路由

| 路由 | 页面 |
|---|---|
| `/terminal`（默认） | 多维甄选台（漏斗、热度、状态区、单币过程） |
| `/quotes` | 合约宇宙报价 |
| `/screener` · `/screener-x` · `/screener-y` | 选币榜 / 选币榜X / 选币榜Y（同一组件，`board` 不同） |
| `/review` | 复盘选币 |
| `/markets` | 合约清单 |
| `/market/:symbol` | 单币 K 线（Lightweight Charts ↔ KLineChart） |

## 命令

以下命令均在 `apps/web/` 目录执行：

```bash
npm ci               # 按 package-lock.json 安装依赖
npm run dev          # 开发服 http://127.0.0.1:5173，/api → VITE_PROXY_TARGET（默认 http://127.0.0.1:18080）
npm run build        # tsc -b && vite build → dist/
npm run preview      # 本地预览 dist/
npm run lint         # oxlint
npm run verify       # 全部前端验证闸
```

单个验证闸：`verify:mcap-sort`、`verify:period-returns`、`verify:pool-counts`、`verify:review-counts`、
`verify:review-time`、`verify:board-split`、`verify:mcap-zone`、`verify:onlycoin`。
验证闸源码在 `tests/*.verify.tsx`，由 rolldown 打包到 `tests/.out/` 后用 Node 运行。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `VITE_API_BASE` | `/api/v1` | API 前缀，例如 `http://127.0.0.1:18080/api/v1` |
| `VITE_USE_MOCK` | 关 | `1` / `true` 时使用 `src/mocks` 的模拟快照，无需后端 |
| `VITE_PROXY_TARGET` | `http://127.0.0.1:18080` | 开发服 `/api` 反代目标 |

## 目录

```text
src/
  App.tsx                 路由
  pages/                  页面（Screener / Review / Markets / Market / Quotes）
  features/               screener · review · workspace · chart · onlycoin 等功能模块
  shared/                 api · config · hooks · lib · stores · types
  mocks/                  离线模拟数据
tests/                    前端验证闸
```

## 署名

渲染 TradingView Lightweight Charts 的页面必须保留指向 <https://www.tradingview.com> 的可见署名；
展示 CoinGecko 数据时须标注 “Powered by CoinGecko”。详见仓库根目录 `NOTICE`。
