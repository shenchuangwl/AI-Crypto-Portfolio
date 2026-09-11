import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchOnlyCoinLive } from '../../shared/api/onlycoin';
import type { OnlyCoinLive } from '../../shared/types/onlycoin';

/** Same 15s poll/online/visibility refresh as the Y cumulative panel. */
export function useOnlyCoinLive() {
  const [data, setData] = useState<OnlyCoinLive | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [, tick] = useState(0);
  const generation = useRef(0);
  const pending = useRef(false);
  const lease = useRef({ end: 0, monotonic: 0, wall: 0 });
  const load = useCallback(async () => {
    if (pending.current) return;
    pending.current = true;
    const id = generation.current;
    const started = performance.now(), wall = Date.now();
    setBusy(true);
    try {
      const value = await fetchOnlyCoinLive();
      if (id !== generation.current) return;
      if (value.valid !== true || value.stale || value.board_key !== 'y' || value.version !== 'param-v2.0.0-screener-y') throw new Error('实时来源无效');
      const remaining = Date.parse(value.valid_until_utc) - Date.parse(value.server_time);
      if (!Number.isFinite(remaining) || remaining <= 0) throw new Error('实时来源已过期');
      lease.current = { end: started + remaining, monotonic: started, wall };
      setData(value); setError('');
    } catch (e) {
      if (id === generation.current) { setData(null); setError((e as Error).message); }
    } finally {
      pending.current = false;
      if (id === generation.current) setBusy(false);
    }
  }, []);
  useEffect(() => {
    void load();
    const poll = window.setInterval(() => void load(), 15000);
    const clock = window.setInterval(() => tick(n => n + 1), 250);
    const wake = () => { tick(n => n + 1); void load(); };
    window.addEventListener('online', wake); document.addEventListener('visibilitychange', wake);
    return () => { generation.current++; clearInterval(poll); clearInterval(clock); window.removeEventListener('online', wake); document.removeEventListener('visibilitychange', wake); };
  }, [load]);
  const now = Math.max(performance.now(), lease.current.monotonic + Math.max(0, Date.now() - lease.current.wall));
  const valid = !!data && now < lease.current.end;
  return { data: valid ? data : null, error, busy, expired: !!data && !valid, load };
}
