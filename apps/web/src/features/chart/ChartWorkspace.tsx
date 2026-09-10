import { useState } from 'react';
import type { ChartEngine } from './ChartAdapter';
import type { ChartOverlay, OhlcvBar } from '../../shared/types/screener';
import { LightweightChartAdapter } from './LightweightChartAdapter';
import { KLineChartAdapter } from './KLineChartAdapter';

interface Props {
  candles: OhlcvBar[];
  overlays?: ChartOverlay[];
  height?: number;
  defaultEngine?: ChartEngine;
  source?: string;
  pricePrecision?: number;
}

export function ChartWorkspace({
  candles,
  overlays,
  height = 440,
  defaultEngine = 'lightweight-charts',
  source,
  pricePrecision,
}: Props) {
  const [engine, setEngine] = useState<ChartEngine>(defaultEngine);
  return (
    <div className="chart-workspace">
      <div className="chart-engine-bar">
        <button
          type="button"
          className={engine === 'lightweight-charts' ? 'on' : ''}
          onClick={() => setEngine('lightweight-charts')}
        >
          LWC 分析
        </button>
        <button
          type="button"
          className={engine === 'klinecharts' ? 'on' : ''}
          onClick={() => setEngine('klinecharts')}
        >
          KLine 终端
        </button>
        {engine === 'klinecharts' ? (
          <span className="chart-engine-note">指标 · 画线 · 磁吸 · 选币锚点</span>
        ) : (
          <span className="chart-engine-note">轻量看盘 + 锚点线</span>
        )}
        {source ? <span className="live-pill">{source.toUpperCase()}</span> : null}
      </div>
      {engine === 'lightweight-charts' ? (
        <LightweightChartAdapter
          candles={candles}
          overlays={overlays}
          height={height}
          pricePrecision={pricePrecision}
        />
      ) : (
        <KLineChartAdapter
          candles={candles}
          overlays={overlays}
          height={height}
          pricePrecision={pricePrecision}
        />
      )}
    </div>
  );
}
