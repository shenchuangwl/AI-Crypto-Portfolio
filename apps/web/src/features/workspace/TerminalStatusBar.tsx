import { useEffect, useState } from 'react';
import { useTickerStore } from '../../shared/stores/tickerStore';
import { usePricePoll } from '../../shared/hooks/usePricePoll';
import { fetchScreenerMeta, type ScreenerMetaLite } from '../../shared/api/screener';

interface HealthBody {
  status?: string;
  selection_latest_exists?: boolean;
  use_live_selection?: boolean;
  loop_status?: { status?: string; last_scan_id?: string; next_due_utc?: string };
}

interface UniverseBody {
  count?: number;
  source?: string;
}

const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '') || '/api/v1';

export function TerminalStatusBar() {
  usePricePoll(8000);
  const btc = useTickerStore((s) => s.quotes.BTCUSDT);
  const [health, setHealth] = useState<HealthBody | null>(null);
  const [uni, setUni] = useState<UniverseBody | null>(null);
  const [scan, setScan] = useState<ScreenerMetaLite | null>(null);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const [h, u, s] = await Promise.all([
          fetch(`${API_BASE}/health`).then((r) => (r.ok ? r.json() : null)),
          fetch(`${API_BASE}/markets/universe`).then((r) => (r.ok ? r.json() : null)),
          fetchScreenerMeta('main').catch(() => null),
        ]);
        if (stop) return;
        setHealth(h);
        setUni(u);
        setScan(s);
      } catch {
        /* keep last */
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 15000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, []);

  const states = scan?.state_counts || {};
  const occ = scan?.occupancy;
  const daily = scan?.daily_unique;
  const last = btc?.last ?? btc?.mark;
  return (
    <div className="term-status">
      <span className={`live-dot${health?.status === 'ok' ? ' on' : ''}`} />
      <b>工作台</b>
      <span>宇宙 {uni?.count ?? '—'}</span>
      <span>BTC {last != null ? last.toFixed(1) : '—'}</span>
      <span>scan {scan?.scan_id ?? '—'}</span>
      <span>WATCH {states.WATCH ?? 0}侧</span>
      <span>QUAL {states.QUALIFIED ?? 0}侧</span>
      <span>CONF {states.CONFIRMED ?? 0}侧</span>
      <span title="瞬时去重占用 / 今日去重入选（计划 §1.3）">
        占用 {occ?.confirmed_unique ?? 0} · 今日 {daily?.confirmed ?? '—'}
      </span>
      <span className="muted">
        {health?.use_live_selection ? 'live snapshot' : 'example'} · {uni?.source || '—'}
      </span>
    </div>
  );
}
