import { useEffect, useRef, useState } from 'react';
import { fetchOnlyCoinReview } from '../../shared/api/onlycoin';
import type { OnlyCoinDaily } from '../../shared/types/onlycoin';
import { OnlyCoinLists } from './OnlyCoinLists';
import { OnlyCoinLivePanel } from './OnlyCoinLivePanel';
import { onlyCoinLocalTime, onlyCoinCycleEndInput } from '../../shared/lib/onlycoin';
import { OnlyCoinStatsPanel } from './OnlyCoinStatsPanel';
export function OnlyCoinReview() {
  const [date, setDate] = useState('');
  const [asOf, setAsOf] = useState('');
  const [data, setData] = useState<OnlyCoinDaily | null>(null);
  // 已完成回放的业务日与截至时点。**只在查询成功后才写**，所以输入框里的预填值
  // 不会被当成「已选择日期」；清空 / 改日期时先归零，统计区随之整块隐藏。
  const [replayed, setReplayed] = useState<{ date: string; asOf: string } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const requestId = useRef(0);
  useEffect(() => () => { requestId.current++; }, []);
  const clear = () => { requestId.current++; setData(null); setReplayed(null); setBusy(false); setError(''); };
  const load = async () => {
    const id = ++requestId.current; setBusy(true); setData(null); setReplayed(null); setError('');
    try {
      const utc = onlyCoinLocalTime(asOf);
      if (utc.slice(0, 10) !== date) throw new Error('截至时间必须位于该业务周期：本地当日07:00至次日07:00之前。');
      const value = await fetchOnlyCoinReview(date, utc);
      if (id === requestId.current) {
        if (value.board_key !== 'y' || value.business_date !== date) throw new Error('复盘业务日 / 板面不匹配');
        setData(value);
        setReplayed({ date, asOf: utc });
      }
    } catch (e) { if (id === requestId.current) setError((e as Error).message); }
    finally { if (id === requestId.current) setBusy(false); }
  };
  return <><OnlyCoinLivePanel /><section className="onlycoin-panel onlycoin-review-panel" aria-busy={busy}>
    <div className="onlycoin-heading">
      <div><span className="confirmed-kicker">DMR区 · 精选复盘</span><h2><strong>{data ? data.counts.onlycoin : '—'}</strong> OnlyCoin 精选标的 <small>来源候选时点回放</small></h2></div>
      <span className="onlycoin-version">v2.0.0 · 选币榜Y</span>
    </div>
    <p className="onlycoin-note">UTC 业务日 · 首次入选排序 · 与选币榜Y共用名单展示，不代表实际成交或策略收益</p>
    <form className="onlycoin-query" onSubmit={e => { e.preventDefault(); if (date && asOf && !busy) void load(); }}>
      <label>业务日（本地07:00起）<input aria-label="UTC 业务日" type="date" required value={date} onChange={e => { clear(); const day = e.target.value; setDate(day); setAsOf(day ? onlyCoinCycleEndInput(day) : ''); }} /></label>
      <label>回放截至时间（本地 UTC+7）<input aria-label="回放截至时间（本地 UTC+7）" type="datetime-local" step="1" required value={asOf} onChange={e => { clear(); setAsOf(e.target.value); if (e.target.value) { try { setDate(onlyCoinLocalTime(e.target.value).slice(0, 10)); } catch { /* Validate on submit. */ } } }} /></label>
      <button className="onlycoin-query-button" type="submit" disabled={!date || !asOf || busy}>{busy ? '查询中…' : '查询 / 刷新回放'}</button>
    </form>
    <details className="onlycoin-coverage onlycoin-review-help"><summary>时间口径与数据来源说明</summary><p>显示与输入同原复盘及选币榜Y，统一为本地 UTC+7；提交时转换为UTC。业务日仍按00:00 UTC划分，对应本地当日07:00至次日07:00之前。左侧选业务日，右侧选截至时点，不是起止日期范围。首次入选不等于当前状态最近入区，首次可用时间用于时点过滤；不改写历史、不代表成交或策略收益。</p></details>
    {error && <p className="onlycoin-feedback onlycoin-feedback-error" role="alert">回放不可用：{error}</p>}
    {!data && !error && <div className="onlycoin-feedback" role="status">{busy ? '正在读取所选时点的候选名单…' : '选择 UTC 业务日及截至时间，查询 LONG、SHORT 与 OnlyCoin 首次入选名单。'}</div>}
    {data && !data.onlycoin.length && <div className="onlycoin-feedback" role="status">该时点暂无可显示的成员，请结合下方覆盖度确认数据是否完整。</div>}
    {data && <OnlyCoinLists data={data} originTag="review-onlycoin" />}
  </section>
  {/* 历史数据统计：只有「已选择具体回放日期且回放加载完成」时才存在。
      key 绑定回放身份 —— 换日期即重挂载，上一日期的行不可能残留。 */}
  {replayed && <OnlyCoinStatsPanel key={`${replayed.date}|${replayed.asOf}`} businessDate={replayed.date} asOf={replayed.asOf} />}
  </>;
}
