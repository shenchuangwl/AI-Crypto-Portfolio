/**
 * 选币榜表格的列结构 —— 列顺序 / 表头文案 / sortKey / 每列取哪个单元格的唯一事实源。
 *
 * 与网格外壳分开有两个原因：`CandidateDataGrid.tsx` 因此只导出组件（fast refresh 才生效），
 * 且表格是虚拟滚动的、SSR 下渲染不出行，验证闸只能从这里逐列取 `render` 验单元格。
 */
import type { ReactNode } from 'react';
import type { CandidateRow, McapTimeframe } from '../../../shared/types/screener';
import { MCAP_TIMEFRAME_LABEL, STATE_LABEL,
  MCAP_ZONE_LABEL,
} from '../../../shared/types/screener';
import type { ColumnPreset, SortId } from '../../../shared/stores/screenerUi';
import { formatCompact, formatDwell, formatEnterClock, formatGrade, formatNum } from '../../../shared/lib/format';
import { MCAP_SORT_ID } from '../../../shared/lib/mcapGrade';
import {
  EnterPrice,
  LiveLast,
  McapGradeCell,
  Ret,
  StayPnlPct,
  StayPnlSign,
} from './candidateCells';

export type Col = {
  id: string;
  label: string;
  width: number;
  render: (r: CandidateRow) => ReactNode;
  sortKey?: SortId;
};

/** 三列共用同一套 A–F 判级，只是取数周期不同 —— 所以列定义也由同一个工厂产出。 */
const MCAP_TF_COLUMNS: Col[] = (['30m', '2h', '6h'] as McapTimeframe[]).map((tf) => ({
  id: MCAP_SORT_ID[tf],
  label: MCAP_TIMEFRAME_LABEL[tf],
  width: 110,
  // 可排序：按 A→F 的等级阶梯，判不出级排最后（降序）
  sortKey: MCAP_SORT_ID[tf],
  render: (r: CandidateRow) => <McapGradeCell row={r} tf={tf} />,
}));

/** 216 天花板层的四列：组合 / Z / 共振K / 天花板。主导层关闭时全部渲染为 `—`。 */
const MCAP_ZONE_COLUMNS: Col[] = [
  {
    id: 'mcap_combo',
    label: '216组合',
    width: 92,
    sortKey: 'mcap_combo',
    render: (r: CandidateRow) =>
      r.mcap_combo_code ? (
        <span className="combo" title={`组合号 ${r.mcap_combo_no ?? '—'} / 216${
          r.mcap_combo_status === 'INCOMPLETE' ? '（三周期等级不全，按观察保守封顶）' : ''
        }`}>
          {r.mcap_combo_code}
          {r.mcap_combo_no != null && <i className="muted"> #{r.mcap_combo_no}</i>}
        </span>
      ) : (
        <span className="muted">—</span>
      ),
  },
  {
    id: 'mcap_z',
    label: 'Z',
    width: 64,
    sortKey: 'mcap_z',
    render: (r: CandidateRow) =>
      r.mcap_z10 == null ? (
        <span className="muted">—</span>
      ) : (
        <span
          className={r.mcap_z10 > 0 ? 'up' : r.mcap_z10 < 0 ? 'down' : 'muted'}
          title={`Z10=${r.mcap_z10}（= 2·q30m + 3·q2h + 5·q6h，值域 −30…+30）；优先级 ${
            r.mcap_priority ?? '—'
          }/5`}
        >
          {(r.mcap_z10 / 10).toFixed(1)}
        </span>
      ),
  },
  {
    id: 'mcap_k',
    label: 'K',
    width: 56,
    sortKey: 'mcap_k',
    render: (r: CandidateRow) =>
      r.mcap_resonance_k == null ? (
        <span className="muted">—</span>
      ) : (
        <span
          className={`k k-${r.mcap_resonance_k}`}
          title="三周期结构共振度：100=三周期同向，70=6h与2h同向，40=6h与30m同向，10=6h孤立"
        >
          {r.mcap_resonance_k}
        </span>
      ),
  },
  {
    id: 'mcap_ceiling',
    label: '天花板',
    width: 88,
    sortKey: 'mcap_ceiling',
    render: (r: CandidateRow) => {
      if (!r.mcap_ceiling_zone) return <span className="muted">—</span>;
      // 天花板严于当前分区 = 这一行正被流通市值压着；标出来才能一眼看到主导关系。
      const capped = r.final_zone != null && r.final_zone !== r.base_state;
      const why = [r.mcap_predicate, r.mcap_crosscheck].filter(Boolean).join(' / ');
      return (
        <span
          className={`zone zone-${r.mcap_ceiling_zone}${capped ? ' capped' : ''}`}
          title={[
            `216 天花板：${r.mcap_ceiling_zone}`,
            r.base_state ? `状态机分区：${r.base_state}` : '',
            r.effective_zone ? `合并后：${r.effective_zone}` : '',
            r.final_zone ? `最终分区：${r.final_zone}` : '',
            why ? `命中：${why}` : '',
            r.rank_key != null ? `RankKey ${r.rank_key.toFixed(4)}（W_final ${r.w_final?.toFixed(3) ?? '—'}）` : '',
          ]
            .filter(Boolean)
            .join('\n')}
        >
          {MCAP_ZONE_LABEL[r.mcap_ceiling_zone]}
          {capped && <i className="capped-mark" title="流通市值把它压低了">▼</i>}
        </span>
      );
    },
  },
];

export function buildCols(preset: ColumnPreset, direction: 'up' | 'down'): Col[] {
  const score = (r: CandidateRow) => (direction === 'up' ? r.score_up : r.score_down);
  const base: Col[] = [
    { id: 'rank', label: '#', width: 48, sortKey: 'rank', render: (r) => r.rank },
    {
      id: 'symbol',
      label: '合约',
      width: 120,
      render: (r) => (
        <span className="sym">
          <b>{r.symbol}</b>
          {r.contract_multiplier > 1 && <i>×{r.contract_multiplier}</i>}
        </span>
      ),
    },
    {
      id: 'state',
      label: '状态',
      width: 96,
      render: (r) => (
        <span className={`state-badge state-${r.state.toLowerCase()}`}>
          {STATE_LABEL[r.state]}
          {r.confirmed_path || r.qualified_path ? (
            <i className="path-tag"> {r.confirmed_path || r.qualified_path}</i>
          ) : null}
        </span>
      ),
    },
    {
      id: 'enter_at',
      label: '入选时间',
      width: 88,
      // 优先读展示区进入时刻（zone_enter_*）：用户看到的是哪个区，时间就跟着哪个区。
      // 缺失时回退 state_enter_*（主榜 v1.4.0 与主导层关闭时走这条路）。
      render: (r) => (
        <span title={r.zone_enter_time_utc || r.state_enter_time_utc || '无进入时刻'}>
          {formatEnterClock(r.zone_enter_time_utc ?? r.state_enter_time_utc)}
        </span>
      ),
    },
    {
      id: 'duration',
      label: '停留时间',
      width: 88,
      render: (r) => (
        <span
          title={`当前分区 ${Number(
            r.zone_duration_minutes ?? r.state_duration_minutes ?? 0,
          ).toFixed(1)} 分钟`}
        >
          {formatDwell(r.zone_duration_minutes ?? r.state_duration_minutes)}
        </span>
      ),
    },
    {
      id: 'enter_px',
      label: '停留价格',
      width: 88,
      render: (r) => <EnterPrice row={r} />,
    },
    {
      id: 'last',
      label: 'Last*',
      width: 88,
      render: (r) => <LiveLast symbol={r.symbol} fallback={r.last_price ?? r.ref_price} />,
    },
    {
      id: 'score',
      label: direction === 'up' ? 'Score↑' : 'Score↓',
      width: 88,
      sortKey: 'score',
      render: (r) => (
        <span className="score-cell">
          <b>{formatNum(score(r), 1)}</b>
          <small>{formatNum(direction === 'up' ? r.score_down : r.score_up, 1)}</small>
        </span>
      ),
    },
    {
      id: 'dir_conf',
      label: 'DirConf',
      width: 72,
      sortKey: 'dir_conf',
      render: (r) => formatNum(r.direction_confidence, 2),
    },
    {
      id: 'grade',
      label: '等级',
      width: 72,
      sortKey: 'grade',
      render: (r) => (
        <span
          className={`grade grade-${r.liquidity_grade}`}
          title={r.liquidity_grade === 'UNCLASSIFIED' ? '6/12/26日成交额过近，无法判 A–D' : r.liquidity_grade}
        >
          {formatGrade(r.liquidity_grade)}
        </span>
      ),
    },
    // §一/§二：等级 与 1h 之间，固定顺序 30m流通市值 → 2h流通市值 → 6h流通市值。
    // 七个分区（DMR/确认/符合/观察/淘汰/数据不足/低置信度）共用这张表，列结构一致。
    ...MCAP_TF_COLUMNS,
    // —— 216 组合天花板：把三周期等级与它决定的分区连起来（y-v2.0.0-r3）——
    //
    // 三列等级只说「是什么形态」，这四列说「这个形态把它压到哪一区」。
    // 没有它们，「流通市值主导」在栏目里就是不可核的。
    // 主导层关闭时后端不下发这些字段，渲染为 `—`，列结构不变。
    ...MCAP_ZONE_COLUMNS,
    {
      id: 'ret_1h',
      label: '1h',
      width: 76,
      sortKey: 'ret_1h',
      render: (r) => <Ret v={r.ret_1h} />,
    },
    {
      id: 'ret_4h',
      label: '4h',
      width: 76,
      sortKey: 'ret_4h',
      render: (r) => <Ret v={r.ret_4h} />,
    },
    {
      id: 'ret_24h',
      label: '24h',
      width: 76,
      sortKey: 'ret_24h',
      render: (r) => <Ret v={r.ret_24h} />,
    },
    // §一：24h 与 锚点以来 之间，固定顺序 1Week → 1Month。取数逻辑与 1h/4h/24h 同源，
    // 只是周期不同；七个分区的上涨/下跌两个候选池共用这张表，列结构一致。
    {
      id: 'ret_1w',
      label: '1Week',
      width: 76,
      sortKey: 'ret_1w',
      render: (r) => <Ret v={r.ret_1w} />,
    },
    {
      id: 'ret_1mo',
      label: '1Month',
      width: 76,
      sortKey: 'ret_1mo',
      render: (r) => <Ret v={r.ret_1mo} />,
    },
    {
      id: 'ret_anchor',
      label: '锚点以来',
      width: 88,
      sortKey: 'ret_anchor',
      render: (r) => <Ret v={r.ret_since_anchor} />,
    },
    {
      id: 'pnl_pct',
      label: '盈亏百分比',
      width: 88,
      sortKey: 'pnl_pct',
      render: (r) => <StayPnlPct row={r} direction={direction} />,
    },
    {
      id: 'pnl_sign',
      label: '盈亏数值',
      width: 72,
      sortKey: 'pnl_sign',
      render: (r) => <StayPnlSign row={r} direction={direction} />,
    },
    {
      id: 'risk',
      label: '风险',
      width: 80,
      render: (r) =>
        r.risk_flags.length ? (
          <span className="risk-flags">{r.risk_flags.join(' ')}</span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      id: 'reasons',
      label: '入选原因',
      width: 160,
      render: (r) => <span className="tags">{r.reason_codes.slice(0, 3).join(' · ') || '—'}</span>,
    },
  ];

  if (preset === 'trader' || preset === 'full_s37') {
    // After DirConf, before 等级 — compact: #/合约/状态/入选时间/停留时间/停留价格/Last/Score/DirConf
    base.splice(
      9,
      0,
      {
        id: 's_l',
        label: 'S_L',
        width: 56,
        sortKey: 'liquidity',
        render: (r) => formatNum(r.liquidity_score, 0),
      },
      {
        id: 'm',
        label: 'M',
        width: 56,
        render: (r) => formatNum(r.momentum_score, 0),
      },
      {
        id: 'ss',
        label: 'SS',
        width: 56,
        sortKey: 'ss',
        render: (r) => formatNum(r.staircase_score, 0),
      },
      {
        id: 'aqv',
        label: 'AQV6/12/26',
        width: 110,
        render: (r) => (
          <span className="aqv">
            {r.aqv_6d_m}/{r.aqv_12d_m}/{r.aqv_26d_m}
          </span>
        ),
      },
      {
        id: 'data_mode',
        label: 'data',
        width: 90,
        render: (r) => <span className={`badge badge-${r.data_mode.toLowerCase()}`}>{r.data_mode}</span>,
      },
    );
  }

  if (preset === 'full_s37') {
    base.push(
      {
        id: 'smc',
        label: 'S_MC',
        width: 56,
        render: (r) => formatNum(r.mcap_momentum_score, 0),
      },
      {
        id: 'sdq',
        label: 'S_DQ',
        width: 56,
        render: (r) => formatNum(r.data_confidence, 0),
      },
      {
        id: 'mcap',
        label: 'M^Calc / CG',
        width: 140,
        render: (r) => (
          <span className="mcap">
            {formatCompact(r.market_cap_calculated)}
            <small>{formatCompact(r.market_cap_coingecko)}</small>
          </span>
        ),
      },
      {
        id: 'supply',
        label: '流通量',
        width: 90,
        render: (r) => formatCompact(r.circulating_supply),
      },
      {
        id: 'not_conf',
        label: '未确认',
        width: 100,
        render: (r) => r.not_confirmed_reasons.join(',') || '—',
      },
    );
  }

  return base;
}
