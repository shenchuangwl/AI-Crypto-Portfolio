import { useCallback, useEffect, useRef, useState, cloneElement, type ReactElement } from 'react';
import { fetchOnlyCoinDaily } from '../../shared/api/onlycoin';
import { dailyExpiry, isDailyCurrent } from '../../shared/lib/onlycoin';
import type { OnlyCoinDaily } from '../../shared/types/onlycoin';
import { OnlyCoinLists } from './OnlyCoinLists';
import { OnlyCoinSourceControl } from './OnlyCoinSourceControl';
export function OnlyCoinPanel({ current, onRefresh }: { current: ReactElement<{ onRefresh?: () => void }>; onRefresh: () => void }) {
  const [mode, setMode] = useState<'daily' | 'current'>('daily');
  const [entry, setEntry] = useState<{ data: OnlyCoinDaily; expiry: number; received: number; wallReceived: number } | null>(null);
  const [error, setError] = useState('');
  const [, tick] = useState(0);
  const alive = useRef(true);
  const pending = useRef(false);
  const load = useCallback(async () => {
    if (pending.current) return;
    pending.current = true;
    const wallReceived = Date.now();
    const started = performance.now(); // Conservative: network transit may not extend the cycle.
    try { const data = await fetchOnlyCoinDaily(); if (alive.current) { setEntry({ data, expiry: dailyExpiry(data, started), received: started, wallReceived }); setError(''); } }
    catch (e) { if (alive.current) setError((e as Error).message); }
    finally { pending.current = false; }
  }, []);
  useEffect(() => {
    alive.current = true; void load();
    const poll = window.setInterval(() => void load(), 15000);
    const clock = window.setInterval(() => tick(n => n + 1), 250);
    const wake = () => { tick(n => n + 1); void load(); };
    window.addEventListener('online', wake); document.addEventListener('visibilitychange', wake);
    return () => { alive.current = false; clearInterval(poll); clearInterval(clock); window.removeEventListener('online', wake); document.removeEventListener('visibilitychange', wake); };
  }, [load]);
  const valid = entry && isDailyCurrent(entry.data, entry.expiry, Math.max(performance.now(), entry.received + Math.max(0, Date.now() - entry.wallReceived)));
  return <section className="onlycoin-panel">
    <div className="onlycoin-heading"><div><span className="confirmed-kicker">DMR区 · 精选执行</span><h2><strong>{valid ? entry.data.counts.onlycoin : '—'}</strong> DMR精选标的 <small>今日去重</small></h2></div><div className="onlycoin-actions">
      <button type="button" aria-pressed={mode === 'daily'} onClick={() => setMode('daily')}>今日累计</button>
      <button type="button" aria-pressed={mode === 'current'} onClick={() => setMode('current')}>当前精选</button>
      <button type="button" onClick={() => { void load(); onRefresh(); }}>刷新</button>
    </div></div>
    <p className="onlycoin-note">00:00 UTC 每日重置 · 首次入选排序 · 仅候选与复盘，不授权交易</p>
    {mode === 'current' ? cloneElement(current, { onRefresh: () => { void load(); onRefresh(); } }) : <>
      {error && <p role="alert">累计取数失败：{error}{valid ? ' · 显示本周期缓存，可能陈旧' : ''}</p>}
      {valid ? <OnlyCoinLists data={entry.data} /> : <p role="status">{entry ? '业务日已到期，旧日成员已隐藏；等待新日数据。' : '等待今日累计数据…'}</p>}
    </>}
    <OnlyCoinSourceControl />
  </section>;
}
