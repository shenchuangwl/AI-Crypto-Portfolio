import { useEffect, useRef } from 'react';
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type UTCTimestamp,
  type SeriesMarker,
  CandlestickSeries,
  HistogramSeries,
  createSeriesMarkers,
} from 'lightweight-charts';
import type { ChartAdapterProps } from './ChartAdapter';
import { minMove, resolvePricePrecision } from '../../shared/lib/price';

function seriesPrecision(candles: ChartAdapterProps['candles'], declared?: number) {
  return resolvePricePrecision({
    declared,
    samples: candles.flatMap((b) => [b.open, b.high, b.low, b.close]),
  });
}

export function LightweightChartAdapter({
  candles,
  overlays = [],
  height = 420,
  pricePrecision,
}: ChartAdapterProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const markersApiRef = useRef<any>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([]);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      height,
      layout: {
        background: { color: '#0b0e11' },
        textColor: '#848e9c',
      },
      grid: {
        vertLines: { color: '#1a1f27' },
        horzLines: { color: '#1a1f27' },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: '#2b3139' },
      timeScale: { borderColor: '#2b3139', timeVisible: true, secondsVisible: false },
    });
    const p0 = seriesPrecision(candles, pricePrecision);
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#0ecb81',
      downColor: '#f6465d',
      borderVisible: false,
      wickUpColor: '#0ecb81',
      wickDownColor: '#f6465d',
      priceFormat: { type: 'price', precision: p0, minMove: minMove(p0) },
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
    });
    chart.priceScale('vol').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;
    seriesRef.current = series;
    volumeRef.current = volume;
    markersApiRef.current = createSeriesMarkers(series, []);

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      volumeRef.current = null;
      markersApiRef.current = null;
    };
  }, [height]);

  useEffect(() => {
    if (!seriesRef.current || !volumeRef.current || !candles.length) return;
    const p = seriesPrecision(candles, pricePrecision);
    seriesRef.current.applyOptions({
      priceFormat: { type: 'price', precision: p, minMove: minMove(p) },
    });
    const data: CandlestickData<UTCTimestamp>[] = candles.map((b) => ({
      time: b.time as UTCTimestamp,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
    seriesRef.current.setData(data);
    volumeRef.current.setData(
      candles.map((b) => ({
        time: b.time as UTCTimestamp,
        value: b.volume,
        color: b.close >= b.open ? 'rgba(14,203,129,0.35)' : 'rgba(246,70,93,0.35)',
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [candles, pricePrecision]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    for (const pl of priceLinesRef.current) {
      try {
        series.removePriceLine(pl);
      } catch {
        /* ignore */
      }
    }
    priceLinesRef.current = [];

    const markers: SeriesMarker<UTCTimestamp>[] = [];

    for (const ov of overlays) {
      if (ov.type === 'anchor_line') {
        const p = ov.payload;
        const pl = series.createPriceLine({
          price: p.price,
          color: '#f0b90b',
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: p.label ?? '00:00 UTC',
        });
        priceLinesRef.current.push(pl);
      }
      if (ov.type === 'confirm_marker') {
        const p = ov.payload;
        markers.push({
          time: p.time as UTCTimestamp,
          position: 'belowBar',
          color: '#0ecb81',
          shape: 'arrowUp',
          text: p.score != null ? `C ${p.score}` : 'CONF',
        });
      }
      if (ov.type === 'entry_arrow') {
        const p = ov.payload;
        const up = p.direction === 'up';
        const listed = p.state === 'CONFIRMED' ? '确认' : '符合';
        markers.push({
          time: p.time as UTCTimestamp,
          position: up ? 'belowBar' : 'aboveBar',
          color: up ? '#0ecb81' : '#f6465d',
          shape: up ? 'arrowUp' : 'arrowDown',
          text: `${listed}${up ? '↑' : '↓'}`,
        });
        const pl = series.createPriceLine({
          price: p.price,
          color: up ? '#0ecb81' : '#f6465d',
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `入选 ${p.price}`,
        });
        priceLinesRef.current.push(pl);
      }
      if (ov.type === 'scan_marker') {
        const p = ov.payload;
        markers.push({
          time: p.time as UTCTimestamp,
          position: 'aboveBar',
          color: '#5e6673',
          shape: 'circle',
          text: p.scan_sequence != null ? `#${p.scan_sequence}` : '',
        });
      }
    }
    markersApiRef.current?.setMarkers(markers);
  }, [overlays, candles]);

  return <div className="chart-host" ref={containerRef} />;
}
