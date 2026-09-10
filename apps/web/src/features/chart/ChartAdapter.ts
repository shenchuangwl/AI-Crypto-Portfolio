import type { ChartOverlay, OhlcvBar } from '../../shared/types/screener';

/** Swap-friendly chart contract. LWC = analysis; KLineChart = terminal indicators. */
export type ChartEngine = 'lightweight-charts' | 'klinecharts';

export interface ChartAdapterProps {
  candles: OhlcvBar[];
  overlays?: ChartOverlay[];
  height?: number;
  /** Exchange / inferred decimal places. Default 2 would misprint 0.01587 as 0.02. */
  pricePrecision?: number;
}

export function pickDefaultEngine(pathname: string): ChartEngine {
  return pathname.startsWith('/market') ? 'klinecharts' : 'lightweight-charts';
}
