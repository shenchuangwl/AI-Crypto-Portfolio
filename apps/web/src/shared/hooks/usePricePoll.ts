import { useEffect } from 'react';
import { useTickerStore, type PricePatch } from '../stores/tickerStore';

const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || '/api/v1';

/** Poll ingest last/mark/24h turnover. Never merge into snapshot Score columns. */
export function usePricePoll(intervalMs = 8000, enabled = true) {
  const merge = useTickerStore((s) => s.merge);
  useEffect(() => {
    if (!enabled) return;
    let stop = false;
    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/market/prices`, { headers: { Accept: 'application/json' } });
        if (!res.ok) return;
        const body = (await res.json()) as {
          source?: string;
          prices?: Record<string, PricePatch>;
        };
        if (!stop && body.prices) merge(body.prices, body.source);
      } catch {
        /* ignore */
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), intervalMs);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [intervalMs, merge, enabled]);
}
