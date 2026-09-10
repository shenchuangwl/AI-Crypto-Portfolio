import { formatEnterClock, formatPct } from '../../shared/lib/format';
import type { ReviewCoverage, ReviewParameterSegment, ReviewSummary } from '../../shared/types/review';
import { fmtRate } from './ReviewSummaryCards';

/** `param-v1.4.0-staircase-confirm-dmr` → `v1.4.0` —— 按钮上只放能一眼分辨的部分。 */
export function shortVersion(v: string): string {
  const m = /v(\d+\.\d+\.\d+)/.exec(v);
  return m ? `v${m[1]}` : v.replace(/^param-/, '');
}

/**
 * 某个参数版本全部交易所横跨的时间区间。
 *
 * 用「最早入选 → 最晚退出」而不是「最早退出 → 最晚退出」：这是一个**超集**，
 * 无论周期归属口径取 exit / enter / contained，都能把该版本的交易全部框进来，
 * 绝不会把边界上的交易裁掉。
 */
export function versionSpan(seg: ReviewParameterSegment): { from: string; to: string } | null {
  const from = seg.from_utc;
  const to = seg.last_exit_utc || seg.to_utc;
  return from && to ? { from, to } : null;
}

/**
 * 参数版本切换条。
 *
 * 跨版本汇总本身没有意义——选入标准变了，两段交易不是同一套规则产生的（§14.4）。
 * 所以这里既是**告警**（当前窗口跨了几个版本），也是**开关**（只看某一套标准自己的成绩）。
 *
 * 只要账本里存在多个版本就常驻显示：一旦筛到单个版本，「本周期跨 N 个版本」的条件
 * 就不再成立，若把开关挂在那个条件上，用户就再也切不回去了。
 */
export function ParamVersionBar({
  cov,
  summary,
  value,
  onChange,
}: {
  cov?: ReviewCoverage;
  summary?: ReviewSummary;
  value: string;
  /** (版本, 该版本数据区间) —— 切换时把窗口一并对齐，否则会切出一片空白 */
  onChange: (v: string, from?: string | null, to?: string | null) => void;
}) {
  const segments = cov?.parameter_segments || [];
  // 单版本账本（选币榜Y）平时不占地方；但 URL 若锁着外板入点
  // （主板 v1.4.0 落到 Y 上），必须把解锁条露出来，否则全 0 且无处可点。
  if (segments.length < 2 && (value === 'all' || !value)) return null;

  const inWindow = summary?.by_parameter_version || {};
  const spanning = Object.keys(inWindow).length > 1;
  const filtered = value !== 'all';
  const locked = filtered ? segments.find((x) => x.version === value) : undefined;
  const lockedSpan = locked ? versionSpan(locked) : null;
  const foreignLock = filtered && !locked;
  // 锁了版本却一笔都没有 = 窗口与该版本的生效区间不重叠，必须说清楚而不是留白。
  const emptyForVersion = filtered && (summary?.trades ?? 0) === 0;

  return (
    <div className={`review-banner param${filtered ? ' filtered' : ''}`}>
      <div className="review-param-head">
        {filtered ? (
          <>
            已锁定参数版本（入点版本） <b>{shortVersion(value)}</b>
            {lockedSpan && (
              <span className="muted">
                {' '}
                · 数据区间 {formatEnterClock(lockedSpan.from)} → {formatEnterClock(lockedSpan.to)}
              </span>
            )}
            <span className="muted">
              {' '}
              {/* v1.3对齐只锁入点：X仍为v1.4克隆；后续演进不能把跨版本留守冒称纯策略成绩。 */}
              —— 下方仅按进入分区时的参数身份过滤；整笔留守可能跨规则。
              请检查 PARAM_VERSION_CHANGED / PARAM_HASH_CHANGED 标记，不能据此认定全程同参。
            </span>
          </>
        ) : spanning ? (
          <>
            ⚠ 本周期跨 <b>{Object.keys(inWindow).length}</b> 个参数版本，选入标准本身已变更，
            跨版本汇总仅供参考 —— 点下面的按钮可只看其中一套（窗口会自动对齐到它的生效区间）。
          </>
        ) : (
          <>账本内共 <b>{segments.length}</b> 个参数版本，可切换只看其中一套。</>
        )}
      </div>

      <div className="review-param-switch">
        <button
          type="button"
          className={`review-param-btn ${value === 'all' ? 'on' : ''}`}
          onClick={() => onChange('all')}
          title="不按参数版本过滤，恢复原来的周期"
        >
          <span className="review-param-btn-name">全部</span>
          <span className="review-param-btn-sub">不过滤</span>
        </button>
        {segments.map((seg) => {
          const win = inWindow[seg.version];
          const on = value === seg.version;
          const span = versionSpan(seg);
          return (
            <button
              key={seg.version}
              type="button"
              className={`review-param-btn ${on ? 'on' : ''}`}
              onClick={() => (on ? onChange('all') : onChange(seg.version, span?.from, span?.to))}
              title={`${seg.version}\n入选区间 ${formatEnterClock(seg.from_utc)} → ${formatEnterClock(seg.to_utc)}\n账本内 ${seg.trades} 笔（全分区）\n点击后窗口自动对齐到该版本的数据区间`}
            >
              <span className="review-param-btn-name">{shortVersion(seg.version)}</span>
              {/* X v1.3.0 是 main v1.4.0 复刻，非旧 dual-path；复盘完整身份常驻消歧，演进只改 X overrides。 */}
              <span className="review-param-btn-sub">{seg.version}</span>
              <span className="review-param-btn-sub">
                {span ? `${formatEnterClock(span.from)} → ${formatEnterClock(span.to)}` : '—'}
              </span>
              <span className="review-param-btn-sub">
                {win
                  ? `${win.trades} 笔 · 胜率 ${fmtRate(win.win_rate)} · 均 ${formatPct(win.avg_pct)}`
                  : `账本 ${seg.trades} 笔（全分区）`}
              </span>
            </button>
          );
        })}
      </div>

      {emptyForVersion && (
        <div className="review-param-empty">
          {foreignLock ? (
            <>
              ⚠ <b>{shortVersion(value)}</b>（{value}）不属于当前账本，对不上任何入点。
              <button type="button" className="linkish" onClick={() => onChange('all')}>
                解除入点锁
              </button>
            </>
          ) : (
            <>
              ⚠ <b>{shortVersion(value)}</b> 在当前周期 / 分区组合下没有任何完整交易。
              {lockedSpan ? (
                <>
                  {' '}它的数据区间是 <b>{formatEnterClock(lockedSpan.from)} → {formatEnterClock(lockedSpan.to)}</b>，
                  与当前窗口不重叠。
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => onChange(value, lockedSpan.from, lockedSpan.to)}
                  >
                    切到该版本的数据区间
                  </button>
                </>
              ) : null}
              <span className="muted">
                {' '}也可能是当前分区组合（如「可执行区 = DMR+确认」）在该版本期间没有产出——DMR 区自
                2026-08-21T17:28Z 才存在。
              </span>
            </>
          )}
        </div>
      )}
    </div>
  );
}
