import { useEffect, useState } from 'react';
import { fetchConfirmedFeed, type ConfirmedFeed } from '../../shared/api/screener';
import { CONFIRMED_OCCUPANCY_LABEL } from '../../shared/config/occupancy';

export function DmrPanel() {
  const [feed, setFeed] = useState<ConfirmedFeed | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchConfirmedFeed()
      .then((c) => {
        if (alive) setFeed(c);
      })
      .catch((e: Error) => alive && setErr(e.message));
    return () => {
      alive = false;
    };
  }, []);

  const long = (feed?.long || []).filter((r) => r.dmr_selected);
  const short = (feed?.short || []).filter((r) => r.dmr_selected);
  const n = long.length + short.length;

  return (
    <div className="dmr-panel">
      <header>
        <h2>DMR区 · 精选执行标的</h2>
        <p className="muted">
          仅展示确认区中再通过 Score/SS/M/C/DQ/LIVE 严格门槛并进入当前 DMR inbox 的标的。
        </p>
      </header>
      {err && <p className="page-status error">{err}</p>}
      <p>
        DMR精选 <b>{n}</b> 侧 · LONG {long.length} · SHORT {short.length}
        {feed?.scan_id ? ` · ${feed.scan_id}` : ''}
      </p>
      <p className="muted">
        {/* 计划 §1.3：占用是 DMR 当刻能吃的集合；日去重是全天轮换总量。两者必须同框。 */}
        确认瞬时占用 <b>{feed?.occupancy?.confirmed_unique ?? 0}</b> 币（目标带 {CONFIRMED_OCCUPANCY_LABEL}） · DMR今日去重{' '}
        <b>{feed?.daily_unique?.dmr ?? '—'}</b> 币 · DMR inbox{' '}
        {feed?.dmr?.inbox_count ?? 0}/K={feed?.dmr?.inbox_k ?? 16}
        {feed?.alerts?.length ? ` · ${feed.alerts.join(' / ')}` : ''}
      </p>
      <ul>
        {long.slice(0, 16).map((r) => (
          <li key={`L-${r.symbol}`}>
            LONG {r.symbol} · {r.score_up?.toFixed?.(1)}
          </li>
        ))}
        {short.slice(0, 16).map((r) => (
          <li key={`S-${r.symbol}`}>
            SHORT {r.symbol} · {r.score_down?.toFixed?.(1)}
          </li>
        ))}
        {!n && <li className="muted">当前无 DMR 精选（确认区 / 符合区见左侧榜）</li>}
      </ul>
    </div>
  );
}
