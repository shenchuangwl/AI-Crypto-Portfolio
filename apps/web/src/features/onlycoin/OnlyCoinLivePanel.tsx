import { useOnlyCoinLive } from './useOnlyCoinLive';
import { OnlyCoinLists } from './OnlyCoinLists';
export function OnlyCoinLivePanel() {
  const { data, error, busy, expired, load } = useOnlyCoinLive();
  return <section className="onlycoin-panel" data-testid="onlycoin-live" aria-busy={busy} aria-label="OnlyCoin 实时精选">
    <div className="onlycoin-heading"><div><span className="confirmed-kicker">DMR区 · 实时来源</span><h2><strong>{data?.counts.onlycoin ?? '—'}</strong> OnlyCoin 实时精选 <small>今日累计 · 自动更新</small></h2></div><div className="onlycoin-actions"><span className="onlycoin-version">v2.0.0 · 选币榜Y</span><button type="button" disabled={busy} onClick={() => void load()}>刷新实时名单</button></div></div>
    <p className="onlycoin-note">与选币榜Y今日累计同源 · 每15秒刷新 · 00:00 UTC重置 · 下方历史条件不影响本区 · 仅候选，不授权交易</p>
    {error && <p className="onlycoin-feedback onlycoin-feedback-error" role="alert">实时来源不可用，已隐藏缓存成员：{error}</p>}
    {!data && !error && <p className="onlycoin-feedback" role="status">{expired ? '实时来源已过期，旧成员已隐藏；等待更新。' : '正在读取实时名单…'}</p>}
    {data && <><p className="muted">状态 {data.status} · 更新 UTC {data.updated_at_utc} · 有效至 UTC {data.valid_until_utc}</p><OnlyCoinLists data={data} originTag="review-onlycoin" /></>}
  </section>;
}
