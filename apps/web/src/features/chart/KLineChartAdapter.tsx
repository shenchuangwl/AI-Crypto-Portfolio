import { useEffect, useRef, useState } from 'react';
import type { Chart, OverlayMode } from 'klinecharts';
import type { ChartAdapterProps } from './ChartAdapter';
import type { ChartOverlay, OhlcvBar } from '../../shared/types/screener';
import { resolvePricePrecision } from '../../shared/lib/price';
import {
  DRAW_TOOLS,
  KLINE_STYLES,
  MAIN_INDICATORS,
  SUB_INDICATORS,
  type MainIndicator,
  type SubIndicator,
} from './klineTheme';

const CANDLE_PANE = 'candle_pane';
const SELECT_GROUP = 'selection';
const MAGNET = { off: 'normal', weak: 'weak_magnet', strong: 'strong_magnet' } as const;
const CANDLE = { solid: 'candle_solid', ohlc: 'ohlc', area: 'area' } as const;

function toKLineData(candles: OhlcvBar[]) {
  return candles.map((b) => ({
    timestamp: b.time * 1000,
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
    volume: b.volume,
  }));
}

function applyPricePrecision(chart: Chart, candles: OhlcvBar[], declared?: number) {
  const p = resolvePricePrecision({
    declared,
    samples: candles.flatMap((b) => [b.open, b.high, b.low, b.close]),
  });
  try {
    chart.setPriceVolumePrecision(p, 0);
  } catch {
    /* ignore */
  }
}

function applySelectionOverlays(chart: Chart, overlays: ChartOverlay[]) {
  try {
    chart.removeOverlay({ groupId: SELECT_GROUP });
  } catch {
    /* empty */
  }
  for (const ov of overlays) {
    if (ov.type === 'anchor_line') {
      chart.createOverlay(
        {
          name: 'simpleTag',
          groupId: SELECT_GROUP,
          lock: true,
          points: [{ value: ov.payload.price }],
          extendData: ov.payload.label ?? '00:00 UTC',
          styles: { line: { color: '#f0b90b', style: 'dashed' as never } },
        },
        CANDLE_PANE,
      );
    }
    if (ov.type === 'confirm_marker') {
      chart.createOverlay(
        {
          name: 'simpleAnnotation',
          groupId: SELECT_GROUP,
          lock: true,
          points: [{ timestamp: ov.payload.time * 1000, value: ov.payload.price }],
          extendData: ov.payload.score != null ? `确认 ${ov.payload.score}` : '确认',
        },
        CANDLE_PANE,
      );
    }
    if (ov.type === 'scan_marker') {
      chart.createOverlay(
        {
          name: 'simpleAnnotation',
          groupId: SELECT_GROUP,
          lock: true,
          points: [{ timestamp: ov.payload.time * 1000 }],
          extendData: ov.payload.scan_sequence != null ? `SCAN#${ov.payload.scan_sequence}` : 'SCAN',
        },
        CANDLE_PANE,
      );
    }
  }
}

export function KLineChartAdapter({
  candles,
  overlays = [],
  height = 420,
  pricePrecision,
}: ChartAdapterProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const pendingRef = useRef<{ candles: OhlcvBar[]; overlays: ChartOverlay[] }>({ candles, overlays });
  const precisionRef = useRef(pricePrecision);
  precisionRef.current = pricePrecision;
  const [ready, setReady] = useState(false);
  const [mainOn, setMainOn] = useState<MainIndicator[]>(['MA']);
  const [subOn, setSubOn] = useState<SubIndicator[]>(['VOL', 'MACD']);
  const [drawTool, setDrawTool] = useState<string | null>(null);
  const [magnet, setMagnet] = useState<OverlayMode>(MAGNET.off as OverlayMode);
  const [candleType, setCandleType] = useState<string>(CANDLE.solid);
  const [hint, setHint] = useState('十字光标看 OHLC · 点画线后在图上落点');

  pendingRef.current = { candles, overlays };

  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    let disposed = false;
    let ro: ResizeObserver | null = null;

    void import('klinecharts').then((mod) => {
      if (disposed || !hostRef.current) return;
      const chart = mod.init(hostRef.current, {
        locale: 'zh-CN',
        timezone: 'UTC',
        styles: KLINE_STYLES,
      });
      if (!chart) return;
      chartRef.current = chart;
      chart.createIndicator('MA', true, { id: CANDLE_PANE });
      chart.createIndicator('VOL', false, { id: 'pane_vol', height: 72 });
      chart.createIndicator('MACD', false, { id: 'pane_macd', height: 80 });
      const pending = pendingRef.current;
      if (pending.candles.length) {
        applyPricePrecision(chart, pending.candles, precisionRef.current);
        chart.applyNewData(toKLineData(pending.candles));
        applySelectionOverlays(chart, pending.overlays);
      }
      ro = new ResizeObserver(() => {
        try {
          chart.resize();
        } catch {
          /* ignore */
        }
      });
      ro.observe(hostRef.current);
      setReady(true);
    });

    return () => {
      disposed = true;
      setReady(false);
      ro?.disconnect();
      const el2 = hostRef.current;
      const chart = chartRef.current;
      chartRef.current = null;
      void import('klinecharts').then((mod) => {
        try {
          if (el2) mod.dispose(el2);
          else if (chart) mod.dispose(chart.id);
        } catch {
          /* ignore */
        }
      });
    };
  }, [height]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !ready || !candles.length) return;
    applyPricePrecision(chart, candles, pricePrecision);
    chart.applyNewData(toKLineData(candles));
    applySelectionOverlays(chart, overlays);
  }, [candles, overlays, ready, pricePrecision]);

  const toggleMain = (name: MainIndicator) => {
    const chart = chartRef.current;
    if (!chart) return;
    const on = mainOn.includes(name);
    if (on) {
      chart.removeIndicator(CANDLE_PANE, name);
      setMainOn((xs) => xs.filter((x) => x !== name));
    } else {
      chart.createIndicator(name, true, { id: CANDLE_PANE });
      setMainOn((xs) => [...xs, name]);
    }
  };

  const toggleSub = (name: SubIndicator) => {
    const chart = chartRef.current;
    if (!chart) return;
    const paneId = `pane_${name.toLowerCase()}`;
    const on = subOn.includes(name);
    if (on) {
      chart.removeIndicator(paneId, name);
      setSubOn((xs) => xs.filter((x) => x !== name));
    } else {
      chart.createIndicator(name, false, { id: paneId, height: name === 'VOL' ? 72 : 80 });
      setSubOn((xs) => [...xs, name]);
    }
  };

  const startDraw = (name: string) => {
    const chart = chartRef.current;
    if (!chart) return;
    setDrawTool(name);
    setHint(`正在画「${DRAW_TOOLS.find((t) => t.id === name)?.label ?? name}」· 在图上依次点击落点`);
    chart.createOverlay({
      name,
      mode: magnet,
      onDrawEnd: () => {
        setDrawTool(null);
        setHint('画线完成 · 可拖点调整，或再点工具继续画');
        return true;
      },
    });
  };

  const clearDrawings = () => {
    const chart = chartRef.current;
    if (!chart) return;
    for (const t of DRAW_TOOLS) {
      try {
        chart.removeOverlay({ name: t.id });
      } catch {
        /* ignore */
      }
    }
    setDrawTool(null);
    applySelectionOverlays(chart, overlays);
    setHint('已清手绘，选币锚点保留');
  };

  const setMagnetMode = (mode: OverlayMode) => {
    setMagnet(mode);
    chartRef.current?.overrideOverlay({ mode });
  };

  const setType = (type: string) => {
    setCandleType(type);
    chartRef.current?.setStyles({ candle: { type: type as never } });
  };

  return (
    <div className="kline-terminal">
      <div className="kline-toolbar" role="toolbar" aria-label="KLine 终端工具">
        <div className="kline-tool-group">
          <span className="kline-tool-label">主图</span>
          {MAIN_INDICATORS.map((name) => (
            <button
              key={name}
              type="button"
              className={mainOn.includes(name) ? 'on' : ''}
              onClick={() => toggleMain(name)}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="kline-tool-group">
          <span className="kline-tool-label">副图</span>
          {SUB_INDICATORS.map((name) => (
            <button
              key={name}
              type="button"
              className={subOn.includes(name) ? 'on' : ''}
              onClick={() => toggleSub(name)}
            >
              {name}
            </button>
          ))}
        </div>
        <div className="kline-tool-group">
          <span className="kline-tool-label">画线</span>
          {DRAW_TOOLS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={drawTool === t.id ? 'on' : ''}
              onClick={() => startDraw(t.id)}
            >
              {t.label}
            </button>
          ))}
          <button type="button" onClick={clearDrawings}>
            清线
          </button>
        </div>
        <div className="kline-tool-group">
          <span className="kline-tool-label">磁吸</span>
          {(
            [
              [MAGNET.off as OverlayMode, '关'],
              [MAGNET.weak as OverlayMode, '弱'],
              [MAGNET.strong as OverlayMode, '强'],
            ] as const
          ).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              className={magnet === mode ? 'on' : ''}
              onClick={() => setMagnetMode(mode)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="kline-tool-group">
          <span className="kline-tool-label">样式</span>
          {(
            [
              [CANDLE.solid, '蜡烛'],
              [CANDLE.ohlc, 'OHLC'],
              [CANDLE.area, '面积'],
            ] as const
          ).map(([type, label]) => (
            <button
              key={type}
              type="button"
              className={candleType === type ? 'on' : ''}
              onClick={() => setType(type)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="kline-hint">{hint}</div>
      <div className="chart-host kline-host" ref={hostRef} style={{ height }} />
    </div>
  );
}
