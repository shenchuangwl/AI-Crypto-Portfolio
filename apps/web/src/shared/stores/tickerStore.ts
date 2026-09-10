import { create } from 'zustand';

export interface TickerQuote {
  last: number | null;
  mark: number | null;
  funding: number | null;
  chg24h: number | null;
  quoteVolume24h: number | null;
  volume24h: number | null;
  updatedAt: number;
}

export interface PricePatch {
  last?: number | null;
  mark?: number | null;
  funding?: number | null;
  chg_24h?: number | null;
  quote_volume_24h?: number | null;
  volume_24h?: number | null;
}

interface TickerState {
  quotes: Record<string, TickerQuote>;
  source: string;
  merge: (prices: Record<string, PricePatch>, source?: string) => void;
  get: (symbol: string) => TickerQuote | undefined;
}

/** Live last/mark/24h turnover only. Must never write Score / state / liquidity columns. */
export const useTickerStore = create<TickerState>((set, get) => ({
  quotes: {},
  source: '',
  merge: (prices, source) => {
    const next = { ...get().quotes };
    const now = Date.now();
    for (const [sym, p] of Object.entries(prices || {})) {
      const prev = next[sym];
      next[sym] = {
        last: p.last ?? prev?.last ?? null,
        mark: p.mark ?? prev?.mark ?? null,
        funding: p.funding ?? prev?.funding ?? null,
        chg24h: p.chg_24h ?? prev?.chg24h ?? null,
        quoteVolume24h: p.quote_volume_24h ?? prev?.quoteVolume24h ?? null,
        volume24h: p.volume_24h ?? prev?.volume24h ?? null,
        updatedAt: now,
      };
    }
    set({ quotes: next, source: source || get().source });
  },
  get: (symbol) => get().quotes[symbol],
}));
