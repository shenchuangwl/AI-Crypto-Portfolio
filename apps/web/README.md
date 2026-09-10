# 选币实时榜 + K 线页 · F1 可运行骨架

对齐文档：
- `~/.hermes/attachments/frontend-screener-kline-component-design-v1.md`
- `~/.hermes/attachments/binance-usdt-perp-auto-coin-selection-v1.2-2.md` §37

## 功能（F1）

- `/screener`：§37 风格顶栏、状态 Chip、上涨/下跌池、列预设、虚拟滚动表
- 默认只显示 **确认 + 符合**（数据不足/低置信度需点 Chip 才混入）
- 点击行进入 `/market/:symbol`：Lightweight Charts K 线 + 选币侧栏
- Mock 数据：约 160 行上涨池，无需后端

## 启动

```bash
cd /data120/screener-web
npm install
npm run dev
```

生产构建：

```bash
npm run build
npm run preview
```

## 目录

```
src/
  pages/ScreenerPage.tsx
  pages/MarketPage.tsx
  features/screener/components/*
  features/chart/LightweightChartAdapter.tsx
  shared/{types,api,stores,lib}
  mocks/generateSnapshot.ts
```

## 下一步（F2+）

- 接真实 `GET /api/v1/screener/latest`
- WS `screener.updated` / ticker
- OrderBook / Trades
- 完整 §37 列与 CSV 导出
