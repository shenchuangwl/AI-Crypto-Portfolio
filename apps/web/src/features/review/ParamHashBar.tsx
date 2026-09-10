import type { ReviewCoverage, ReviewParamHashSegment, ReviewSummary } from '../../shared/types/review';

/** `pf1_fcea251fa94122fe` → `pf1_fcea25…` —— 按钮上只放能一眼分辨的前缀。 */
export function shortHash(h: string | null): string {
  if (!h) return '无指纹';
  return h.length > 12 ? `${h.slice(0, 11)}…` : h;
}

/** 某个调参段全部交易横跨的时间区间（与 versionSpan 同口径：取超集，绝不裁掉边界交易）。 */
export function hashSpan(seg: ReviewParamHashSegment): { from: string; to: string } | null {
  const from = seg.from_utc;
  const to = seg.last_exit_utc || seg.to_utc;
  return from && to ? { from, to } : null;
}

/** store / URL 里用 `'null'` 表示「没有指纹的历史行」，与后端 `?ph=null` 对齐。 */
export function hashKey(h: string | null): string {
  return h == null ? 'null' : h;
}

/**
 * 调参段（param_hash）切换条。
 *
 * 与 `ParamVersionBar` 是**两层**，别混：
 *   ParamVersionBar(pv)  哪一套选入标准（v1.4.0 / v2.0.0）—— 换标准 = 换规则
 *   ParamHashBar(ph)     这套标准的**哪一次调参** —— overrides 每改一次就换一段
 *
 * 为什么必须有它：选币榜Y 的 216 主导层上线前后，`parameter_version` 都是
 * `param-v2.0.0-screener-y`，**只有 param_hash 变**。只有 pv 过滤器时，页面把
 * 「天花板关」与「天花板主导」两段混算成一个胜率 —— 那是文档B §3.1 明令禁止的
 * 跨段聚合，也会让 31 天评审读到错的数。
 *
 * 只要账本里存在多个段就常驻显示：一旦筛到单段，「跨 N 段」的条件就不再成立，
 * 若把开关挂在那个条件上，用户就再也切不回去了（与 ParamVersionBar 同一处教训）。
 */
export function ParamHashBar({
  cov,
  summary,
  value,
  onChange,
}: {
  cov?: ReviewCoverage;
  summary?: ReviewSummary;
  value: string;
  /** (段, 该段数据区间) —— 切换时把窗口一并对齐，否则会切出一片空白 */
  onChange: (v: string, from?: string | null, to?: string | null) => void;
}) {
  const segments = cov?.param_hash_segments || [];
  if (segments.length < 2) return null;

  const filtered = value !== 'all';
  const locked = filtered ? segments.find((x) => hashKey(x.param_hash) === value) : undefined;
  const lockedSpan = locked ? hashSpan(locked) : null;
  // 锁了段却一笔都没有 = 窗口与该段的生效区间不重叠，必须说清楚而不是留白。
  const emptyForHash = filtered && (summary?.trades ?? 0) === 0;

  return (
    <div className={`review-banner param${filtered ? ' filtered' : ''}`}>
      <div className="review-param-head">
        {filtered ? (
          <>
            <b>调参段 {shortHash(locked?.param_hash ?? null)}</b>
            <span className="muted">
              {locked?.param_hash
                ? '只统计这一次调参下产生的交易'
                : '阶段 0 之前没有参数指纹的历史行，不可与任何调参段合并统计'}
            </span>
          </>
        ) : (
          <>
            <b>本账本有 {segments.length} 个调参段</b>
            <span className="muted">
              同一参数版本内部，overrides 每改一次就换一段。跨段汇总没有意义 ——
              规则变了，两段交易不是同一套参数产生的。
            </span>
          </>
        )}
      </div>

      <div className="review-param-btns">
        <button
          type="button"
          className={value === 'all' ? 'active' : ''}
          onClick={() => onChange('all')}
          title="不按调参段过滤（跨段混算，仅供总量核对）"
        >
          全部
        </button>
        {segments.map((seg) => {
          const key = hashKey(seg.param_hash);
          const span = hashSpan(seg);
          return (
            <button
              type="button"
              key={key}
              className={value === key ? 'active' : ''}
              onClick={() => onChange(key, span?.from, span?.to)}
              title={`${seg.param_hash ?? '无指纹（阶段 0 之前）'}\n${seg.parameter_version ?? ''}\n${
                span ? `${span.from} → ${span.to}` : ''
              }\n${seg.trades} 笔`}
            >
              {shortHash(seg.param_hash)}
              <span className="muted"> · {seg.trades}</span>
            </button>
          );
        })}
      </div>

      {emptyForHash && lockedSpan ? (
        <div className="review-param-empty">
          当前窗口内该段没有交易。该段的数据区间是 {lockedSpan.from} → {lockedSpan.to}，
          点上面的按钮会把窗口自动对齐过去。
        </div>
      ) : null}
    </div>
  );
}
