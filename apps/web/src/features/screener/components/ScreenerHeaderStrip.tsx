import type { ScanMeta } from '../../../shared/types/screener';
import {
  CONFIRMED_OCCUPANCY_HI,
  CONFIRMED_OCCUPANCY_LO,
  CONFIRMED_OCCUPANCY_LABEL,
} from '../../../shared/config/occupancy';
import {
  formatEta,
  formatLocal,
  formatUtc,
  hoursSinceAnchor,
  nextScanEtaSec,
} from '../../../shared/lib/format';
import { useEffect, useState } from 'react';

interface Props {
  meta: ScanMeta;
  expiresAt: string;
  userTz?: string;
  /** 页头徽标 / 标题 / 附注 —— 由板面配置注入（选币榜 §37，选币榜Y Y）。
   *  默认值保持「选币榜」原样，未传时渲染结果与本次改动前逐字相同。 */
  brandMark?: string;
  brandTitle?: string;
  brandNote?: string;
}

export function ScreenerHeaderStrip({
  meta,
  expiresAt,
  userTz = 'Asia/Bangkok',
  brandMark = '§37',
  brandTitle = 'Binance USDT 永续 · 自动选币实时榜',
  brandNote,
}: Props) {
  const [eta, setEta] = useState(() => nextScanEtaSec());
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => {
      setEta(nextScanEtaSec());
      setNow(Date.now());
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  const tHours = hoursSinceAnchor(meta.anchor_date, meta.scan_timestamp_utc).toFixed(2);
  const expired = now > Date.parse(expiresAt);
  const credits = meta.coingecko_credits;
  // 计划 §1.3 / §13.4: occupancy and the day roll-up are different denominators.
  // Showing one without the other is how "36 today" gets misread as "36 tradable now".
  const occConfirmed = meta.occupancy?.confirmed_unique ?? 0;
  const occQualified = meta.occupancy?.qualified_unique ?? 0;
  const dailyConfirmed = meta.daily_unique?.confirmed;
  const dailyDmr = meta.daily_unique?.dmr;
  const dailyQualified = meta.daily_unique?.qualified;
  const alerts = meta.alerts ?? (meta.alert ? [meta.alert] : []);
  const occCls =
    occConfirmed > CONFIRMED_OCCUPANCY_HI
      ? 'badge-warn'
      : occConfirmed < CONFIRMED_OCCUPANCY_LO
        ? 'badge-warn'
        : 'badge-ok';
  const creditCls =
    credits.alert_level === 'critical'
      ? 'badge-critical'
      : credits.alert_level === 'warn'
        ? 'badge-warn'
        : 'badge-ok';

  return (
    <header className="header-strip">
      <div className="header-row">
        <div className="brand">
          <span className="brand-mark">{brandMark}</span>
          <div>
            <div className="brand-title">{brandTitle}</div>
            <div className="brand-sub">
              system {meta.system_version} · {meta.parameter_version}
              {brandNote ? <span className="muted"> · {brandNote}</span> : null}
            </div>
          </div>
        </div>
        <div className="header-metrics">
          <Metric label="锚点日" value={`${meta.anchor_date} 00:00 UTC`} />
          <Metric
            label="本地"
            value={formatLocal(meta.scan_timestamp_utc, userTz, 'MM-DD HH:mm')}
          />
          <Metric label="扫描" value={formatUtc(meta.scan_timestamp_utc, 'HH:mm:ss')} />
          <Metric label="scan#" value={`${meta.scan_sequence} · ${meta.scan_id}`} />
          <Metric label="T+" value={`${tHours}h`} />
          <Metric label="下一节点" value={formatEta(eta)} accent />
        </div>
      </div>
      <div className="header-row secondary">
        <span>
          有效 <b>{meta.effective_universe}</b> / 基线 {meta.baseline_universe}
        </span>
        <span>
          regime <b>{meta.regime.toFixed(2)}</b> {meta.regime_label}
        </span>
        <span className={`badge badge-${meta.data_mode.toLowerCase()}`}>{meta.data_mode}</span>
        <span className={`badge ${creditCls}`}>
          CG credits {credits.used_today} · 月末估 {credits.month_est.toLocaleString()}/
          {credits.month_cap.toLocaleString()} ({(credits.utilization * 100).toFixed(1)}%)
        </span>
        {expired && <span className="banner-inline warn">扫描结果已过期（相对 mock 时钟）— 仍展示快照</span>}
      </div>
      <div className="header-row secondary">
        {meta.dmr && (
          <span className="badge badge-ok">
            DMR区 <b>{meta.dmr.inbox_count ?? 0}</b>/K={meta.dmr.inbox_k ?? 16}
            {dailyDmr != null && <> · 今日去重 {dailyDmr}</>}
          </span>
        )}
        <span className={`badge ${occCls}`}>
          确认占用 <b>{occConfirmed}</b>
          {dailyConfirmed != null && <> · 今日去重 {dailyConfirmed}</>} · 目标带{' '}
          {CONFIRMED_OCCUPANCY_LABEL}
        </span>
        <span className="badge badge-ok">
          符合占用 <b>{occQualified}</b>
          {dailyQualified != null && <> · 今日去重 {dailyQualified}</>}
        </span>

        {meta.control && (
          <span className="badge badge-ok">
            n_impulse {meta.control.n_impulse ?? '—'} · LIVE{' '}
            {((meta.control.live_ratio ?? 1) * 100).toFixed(0)}%
          </span>
        )}
        {meta.control?.freeze_new_confirm && (
          <span className="badge badge-critical">冻结新晋确认（应激）</span>
        )}
        {alerts.map((a) => (
          <span key={a} className="badge badge-warn">
            {a}
          </span>
        ))}
      </div>
    </header>
  );
}

function Metric({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div className={`metric ${accent ? 'accent' : ''}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}
