/** Chart timeframes — Binance USDT-M fapi native intervals. */
export const KLINE_INTERVALS = ['15m', '30m', '1h', '2h', '4h', '6h', '1d'] as const;

export type KlineInterval = (typeof KLINE_INTERVALS)[number];

export const DEFAULT_KLINE_INTERVAL: KlineInterval = '15m';

const SEC: Record<KlineInterval, number> = {
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '2h': 7200,
  '4h': 14400,
  '6h': 21600,
  '1d': 86400,
};

/** How many bars to request so the chart has a usable history. */
const LIMIT: Record<KlineInterval, number> = {
  '15m': 300,
  '30m': 300,
  '1h': 300,
  '2h': 240,
  '4h': 240,
  '6h': 200,
  '1d': 365,
};

export function isKlineInterval(v: string | null | undefined): v is KlineInterval {
  return !!v && (KLINE_INTERVALS as readonly string[]).includes(v);
}

export function parseKlineInterval(v: string | null | undefined): KlineInterval {
  return isKlineInterval(v) ? v : DEFAULT_KLINE_INTERVAL;
}

export function intervalSeconds(iv: KlineInterval): number {
  return SEC[iv];
}

export function intervalBarLimit(iv: KlineInterval): number {
  return LIMIT[iv];
}

/** Snap an event unix-seconds timestamp onto the candle whose window contains it. */
export function snapBarTime(
  candles: Array<{ time: number }>,
  eventUnix: number,
  intervalSec: number,
): number | null {
  if (!candles.length || !Number.isFinite(eventUnix) || intervalSec <= 0) return null;
  let lastAtOrBefore: number | null = null;
  for (const b of candles) {
    const t = Number(b.time);
    if (!Number.isFinite(t)) continue;
    if (t <= eventUnix && eventUnix < t + intervalSec) return t;
    if (t <= eventUnix) lastAtOrBefore = t;
  }
  if (lastAtOrBefore != null) return lastAtOrBefore;
  return Number(candles[0].time);
}
