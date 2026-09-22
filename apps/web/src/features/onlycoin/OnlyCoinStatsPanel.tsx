import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { fetchOnlyCoinStats } from '../../shared/api/onlycoin';
import { formatDwell, formatEnterClock, formatPct } from '../../shared/lib/format';
import { formatPrice } from '../../shared/lib/price';
import { localInputToUtcIso, utcIsoToLocalInput } from '../../shared/lib/reviewTime';
import type { OnlyCoinComparePeriod, OnlyCoinStatRow, OnlyCoinStats } from '../../shared/types/onlycoinStats';
import { SummaryCard } from '../review/ReviewSummaryCards';
import { PeriodCompareTable, type ComparePeriodResult } from '../review/PeriodCompareTable';

/** 与原复盘 `MAX_COMPARE_PERIODS` 一致：主区间 + 3 列。 */
export const MAX_ONLYCOIN_COMPARE = 3;

/** 前端停用开关。设为 off 时整块不渲染，服务端 404 时同样整块隐藏。 */
export function onlyCoinStatsEnabled(): boolean {
  const flag = (import.meta.env.VITE_ONLYCOIN_STATS as string | undefined)?.trim().toLowerCase();
  return flag !== 'off' && flag !== '0' && flag !== 'false' && flag !== 'no';
}

/** UTC 起止输入：展示 +07 墙钟，提交转 UTC —— 与原复盘 `RangeInput` 同一对换算函数。 */
function RangeInput({ value, onChange, ariaLabel }: { value: string; onChange: (utcIso: string) => void; ariaLabel: string }) {
  return (
    <input
      type="datetime-local"
      aria-label={ariaLabel}
      value={utcIsoToLocalInput(value)}
      onChange={(e) => onChange(localInputToUtcIso(e.target.value))}
    />
  );
}

const FLAG_LABEL: Record<string, string> = {
  INCOMPLETE_DAY: '交易日未结束',
  MISSING_ENTRY_PRICE: '缺入选行情',
  MISSING_EXIT_PRICE: '缺收线行情',
  EXIT_PRICE_PREV_NODE: '收线节点回溯',
};

function Integrity({ row }: { row: OnlyCoinStatRow }) {
  if (!row.flags.length) return <span className="ret up">完整</span>;
  return (
    <span className="ret dim" title={row.flags.join(' · ')}>
      {row.flags.map((f) => FLAG_LABEL[f] || f).join(' · ')}
    </span>
  );
}

function Pct({ v }: { v: number | null }) {
  if (v == null) return <span className="muted">—</span>;
  return <span className={`ret ${v > 0 ? 'up' : v < 0 ? 'down' : ''}`}>{formatPct(v)}</span>;
}

export function DetailTable({ rows }: { rows: OnlyCoinStatRow[] }) {
  if (!rows.length) {
    return <div className="empty">该统计区间内 0 个唯一币种</div>;
  }
  return (
    <div className="grid-wrap static">
      <table className="review-compare-table onlycoin-stats-table">
        <thead>
          <tr>
            <th>#</th>
            <th>唯一币种</th>
            <th>合约</th>
            <th>候选池 / 方向</th>
            <th>入选时间 (+07)</th>
            <th>停留价格</th>
            <th>退出时间 (+07)</th>
            <th>退出价格</th>
            <th>停留时间</th>
            <th>盈亏百分比</th>
            <th>盈亏数值</th>
            <th>数据完整性</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.coin_id} className={r.status === 'OPEN' ? 'review-open' : undefined}>
              <td className="review-compare-label">{i + 1}</td>
              <td className="review-compare-label" title={`canonical_asset_id ${r.canonical_asset_id || '—'}`}>
                <b>{r.coin_id}</b>
                {r.repeat_entries > 0 && (
                  <span className="muted" title="该唯一币种在本区间内的后续重复入选次数，按口径不另计">
                    {' '}
                    ×{r.repeat_entries + 1}
                  </span>
                )}
              </td>
              <td className="review-compare-label">
                <Link to={`/market/${encodeURIComponent(r.symbol)}?from=review-onlycoin&direction=${r.direction}`}>
                  {r.symbol}
                </Link>
              </td>
              <td className={`ret ${r.direction === 'up' ? 'up' : 'down'}`}>
                {r.pool === 'LONG' ? '上涨候选池 · 做多' : '下跌候选池 · 做空'}
              </td>
              <td title={`UTC ${r.enter_time_utc} · scan ${r.enter_scan_id}`}>{formatEnterClock(r.enter_time_utc)}</td>
              <td title={r.enter_price_source ? `来源 ${r.enter_price_source}` : '缺行情'}>
                {r.enter_price == null ? <span className="muted">—</span> : formatPrice(r.enter_price)}
              </td>
              <td title={r.exit_scan_id ? `UTC ${r.exit_time_utc} · scan ${r.exit_scan_id}` : '该交易日尚未结束'}>
                {r.exit_time_utc ? formatEnterClock(r.exit_time_utc) : <span className="muted">—</span>}
              </td>
              <td title={r.exit_price_source ? `来源 ${r.exit_price_source}` : '缺行情'}>
                {r.exit_price == null ? <span className="muted">—</span> : formatPrice(r.exit_price)}
              </td>
              <td title={r.dwell_nodes == null ? undefined : `${r.dwell_nodes} 个 15m 节点`}>
                {r.dwell_minutes == null ? <span className="muted">—</span> : formatDwell(r.dwell_minutes)}
              </td>
              <td>
                <Pct v={r.pnl_pct} />
              </td>
              <td title="项目既有「盈亏数值」口径 = 方向感知符号 1 / 0 / −1；不含仓位、本金、杠杆与手续费">
                {r.pnl_sign == null ? (
                  <span className="muted">—</span>
                ) : (
                  <span className={`ret ${r.pnl_sign > 0 ? 'up' : r.pnl_sign < 0 ? 'down' : ''}`}>{r.pnl_sign}</span>
                )}
              </td>
              <td>
                <Integrity row={r} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * 《OnlyCoin · 来源候选回放》的 DMR 区历史统计。
 *
 * 显示条件（需求 §3.2 / §3.3）：**必须**同时拿到已完成回放的业务日与截至时点。
 * 父组件只在回放查询成功后才把它们传下来，因此「输入框里有个默认值」不会被误判成
 * 「已选择日期」。父组件还用 `key` 把这块按日期重挂载，旧日期的行不可能残留。
 */
export function OnlyCoinStatsPanel({ businessDate, asOf }: { businessDate: string; asOf: string }) {
  const [useCustom, setUseCustom] = useState(false);
  const [customFrom, setCustomFrom] = useState('');
  const [customTo, setCustomTo] = useState('');
  const [compareOn, setCompareOn] = useState(false);
  const [periods, setPeriods] = useState<OnlyCoinComparePeriod[]>([]);
  const [data, setData] = useState<OnlyCoinStats | null>(null);
  const [error, setError] = useState('');
  const [disabled, setDisabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);

  // 只有起止都填齐才算一个自定义区间；半填时整块回落到回放业务日，绝不半生效。
  const customReady = useCustom && Boolean(customFrom) && Boolean(customTo) && customFrom < customTo;
  const readyPeriods = useMemo(
    () => (compareOn ? periods.filter((p) => p.from && p.to && p.from < p.to) : []),
    [compareOn, periods],
  );
  // 依赖只用原始值：对象字面量每次渲染都是新引用，会把请求打成无限循环。
  const compareKey = readyPeriods.map((p) => `${p.from}~${p.to}`).join('|');

  const load = useCallback(async () => {
    if (!businessDate || !asOf) return;
    const id = ++generation.current;
    setBusy(true);
    // 立刻清空：换日期/换区间时宁可空白，也不要让上一段的数字停留在屏幕上。
    setData(null);
    setError('');
    try {
      const body = await fetchOnlyCoinStats(businessDate, asOf, {
        from: customReady ? customFrom : undefined,
        to: customReady ? customTo : undefined,
        compare: compareKey ? compareKey.split('|').map((s) => ({ from: s.split('~')[0], to: s.split('~')[1] })) : [],
      });
      if (id !== generation.current) return; // 迟到的旧请求，丢弃
      if (body.schema !== 'onlycoin-stats-v1' || body.board_key !== 'y') throw new Error('统计载荷不匹配');
      setData(body);
      setDisabled(false);
    } catch (e) {
      if (id !== generation.current) return;
      const message = (e as Error).message || String(e);
      // 服务端开关关闭 → 整块隐藏，而不是报一个用户无从处理的错误。
      if (/HTTP 404/.test(message)) setDisabled(true);
      else setError(message);
    } finally {
      if (id === generation.current) setBusy(false);
    }
  }, [businessDate, asOf, customReady, customFrom, customTo, compareKey]);

  useEffect(() => {
    void load();
    return () => {
      generation.current++; // 卸载后到达的响应不得写回状态
    };
  }, [load]);

  if (!onlyCoinStatsEnabled() || disabled) return null;

  const main = data?.main;
  const comparePeriods: ComparePeriodResult[] = data
    ? [
        { id: 'main', label: main?.label || '主区间', from: main?.from, to: main?.to, summary: main?.summary },
        ...data.compare.map((p) => ({ id: p.id, label: p.label, from: p.from, to: p.to, summary: p.summary, error: p.error })),
      ]
    : [];

  const addPeriod = () => {
    if (periods.length >= MAX_ONLYCOIN_COMPARE) return;
    setCompareOn(true);
    setPeriods((prev) => [
      ...prev,
      { id: `oc${Date.now()}${prev.length}`, label: `周期 ${prev.length + 2}`, from: '', to: '' },
    ]);
  };

  return (
    <section className="onlycoin-panel onlycoin-stats-panel" aria-busy={busy} aria-label="OnlyCoin 回放历史统计">
      <div className="onlycoin-heading">
        <div>
          <span className="confirmed-kicker">DMR区 · 历史数据统计</span>
          <h2>
            <strong>{main?.summary?.unique_coins ?? '—'}</strong> 唯一币种 <small>首次入选 → 完整交易日收线</small>
          </h2>
        </div>
        <div className="onlycoin-actions">
          <span className="onlycoin-version">v2.0.0 · 选币榜Y</span>
          <button type="button" disabled={busy} onClick={() => void load()}>
            {busy ? '统计中…' : '重新统计'}
          </button>
        </div>
      </div>
      <p className="onlycoin-note">
        统计口径与《原复盘 · 进出账本》的 DMR 区共用同一套内核（同一盈亏函数、同一汇总函数），但成员只取本回放的
        OnlyCoin 来源候选，退出按{data?.node_grid.exit_rule || '首次入选所属完整交易日的收线时点'}。
        同一唯一币种在同一统计区间内只计一次；盈亏不含仓位、本金、杠杆、手续费与资金费。
      </p>

      <div className="review-custom-range onlycoin-stats-controls">
        <label className="toolbar-label">
          <input
            type="checkbox"
            checked={useCustom}
            onChange={(e) => setUseCustom(e.target.checked)}
            aria-label="启用自定义起止"
          />{' '}
          自定义起止
        </label>
        <span className="muted">（本地 UTC+7，提交按 UTC；不勾选时按所选回放业务日统计）</span>
        <RangeInput ariaLabel="统计自定义起点" value={customFrom} onChange={setCustomFrom} />
        <span>—</span>
        <RangeInput ariaLabel="统计自定义终点" value={customTo} onChange={setCustomTo} />
        {(customFrom || customTo) && (
          <button
            type="button"
            className="linkish"
            onClick={() => {
              setCustomFrom('');
              setCustomTo('');
            }}
          >
            清除自定义
          </button>
        )}
        {useCustom && !customReady && <span className="review-misaligned">⚠ 起止都填齐且终点在起点之后才生效，当前仍按回放业务日统计</span>}
        <span className="review-range-sep" />
        <label className="toolbar-label">
          <input
            type="checkbox"
            checked={compareOn}
            onChange={(e) => {
              setCompareOn(e.target.checked);
              if (e.target.checked && !periods.length) addPeriod();
            }}
            aria-label="启用周期对比"
          />{' '}
          周期对比
        </label>
        {compareOn &&
          periods.map((p, i) => (
            <span key={p.id} className="review-compare-period">
              <b>周期 {i + 2}</b>
              <RangeInput
                ariaLabel={`统计对比周期 ${i + 2} 起点`}
                value={p.from}
                onChange={(iso) => setPeriods((prev) => prev.map((x) => (x.id === p.id ? { ...x, from: iso } : x)))}
              />
              <RangeInput
                ariaLabel={`统计对比周期 ${i + 2} 终点`}
                value={p.to}
                onChange={(iso) => setPeriods((prev) => prev.map((x) => (x.id === p.id ? { ...x, to: iso } : x)))}
              />
              <button
                type="button"
                className="linkish"
                onClick={() => {
                  const left = periods.filter((x) => x.id !== p.id);
                  setPeriods(left);
                  setCompareOn(left.length > 0);
                }}
              >
                ×
              </button>
            </span>
          ))}
        {compareOn && periods.length < MAX_ONLYCOIN_COMPARE && (
          <button type="button" className="linkish" onClick={addPeriod}>
            + 添加周期
          </button>
        )}
      </div>

      {error && (
        <p className="onlycoin-feedback onlycoin-feedback-error" role="alert">
          统计不可用：{error}
          <button type="button" className="linkish" onClick={() => void load()}>
            重试
          </button>
        </p>
      )}
      {!data && !error && (
        <div className="onlycoin-feedback" role="status">
          {busy ? '正在按所选区间统计首次入选与完整交易日收线…' : '等待统计结果…'}
        </div>
      )}

      {data && main && (
        <>
          <p className="muted">
            统计区间 {formatEnterClock(main.from)} → {formatEnterClock(main.to)}（本地 UTC+7）· 可用性截至{' '}
            {formatEnterClock(main.cutoff)} · 数据集 {data.dataset_id} · 有提交的业务日{' '}
            {main.days_with_commits?.length ? main.days_with_commits.join('、') : '无'} · 读取板面快照{' '}
            {data.snapshot_reads} 张
            {main.incomplete_days?.length ? ` · 未完成交易日 ${main.incomplete_days.join('、')}（这些币种不计入盈亏）` : ''}
          </p>
          <div className="review-cards">
            <SummaryCard title={`${main.label} · 合并`} s={main.summary} />
            <SummaryCard title="上涨候选池 · 做多" s={main.summary?.by_direction?.up} compact />
            <SummaryCard title="下跌候选池 · 做空" s={main.summary?.by_direction?.down} compact />
          </div>
          <div className="toolbar-meta review-meta">
            唯一币种 <b>{main.summary?.unique_coins ?? 0}</b> · 完整交易日已收线 {main.summary?.trades ?? 0} · 可算盈亏{' '}
            {main.summary?.usable ?? 0} · 未完成 {main.summary?.open_trades ?? 0}
            {main.summary?.missing_px ? ` · 缺行情 ${main.summary.missing_px}` : ' · 缺行情 0'}
            <span className="muted"> · 停留/退出价取板面打印价（last_price → ref_price），与原 DMR 账本同一取价函数</span>
          </div>
          <PeriodCompareTable periods={comparePeriods} />
          <DetailTable rows={main.rows || []} />
        </>
      )}
    </section>
  );
}
