import { formatEnterClock } from '../../shared/lib/format';
import type { ReviewCycleCoverage, ReviewCycleWindow } from '../../shared/types/review';

/** `20260825T0000Z` → `08-25`。周期标识按天读，别让人去数 T0000Z。 */
function cycleLabel(key?: string): string {
  if (!key || key.length < 8) return key ?? '—';
  return `${key.slice(4, 6)}-${key.slice(6, 8)}`;
}

/**
 * 复盘窗口的周期栅格条 —— 只在有时间区的规则版本（v2.0.0 / 选币榜Y）下出现。
 *
 * 为什么必须有这一条：选币榜Y 每 24 小时（00:00 UTC）清空全部分区重建。
 * 复盘窗口如果按「水位往前推 N×24h」算，就会从半夜切开两个周期 ——
 * 既漏掉当前周期的头几个小时，又把上一个周期的尾巴连同它的重置强平算进来。
 * 那样得出的胜率与均值不是任何一套规则的成绩。
 *
 * 所以窗口一律落在周期边界上（服务端按 CycleConfig 算，前端只显示），
 * 这条就负责把「你现在看的到底是哪几个周期」摆在明面上。
 */
export function CycleWindowBar({
  cycle,
  window: win,
  wholeCycles,
  onToggleWhole,
}: {
  cycle?: ReviewCycleCoverage;
  window?: ReviewCycleWindow;
  wholeCycles: boolean;
  onToggleWhole: (v: boolean) => void;
}) {
  if (!cycle?.enabled) return null;

  const partial = Boolean(win?.includes_partial_current);
  const elapsed = cycle.current_elapsed_hours ?? 0;
  const period = cycle.period_hours ?? 24;
  const pct = Math.round((cycle.current_progress ?? 0) * 100);

  return (
    <div className="review-banner cycle-window">
      <div className="review-param-head">
        <span className="cycle-badge">{period}h 周期</span>{' '}
        窗口已对齐到周期边界
        {win ? (
          <>
            ：<b>{cycleLabel(win.first_cycle_key)} → {cycleLabel(win.last_cycle_key)}</b>
            <span className="muted">
              {' '}
              （共 {win.cycles} 个 · {formatEnterClock(win.window_start_utc)} →{' '}
              {formatEnterClock(win.window_end_utc)}）
            </span>
          </>
        ) : null}
        <span className="muted">
          {' '}
          —— 与选币榜Y 的重置刻度是同一张栅格（起点 {cycle.anchor_utc ?? '00:00'} UTC）
        </span>
      </div>

      <div className="review-param-head">
        <span className="muted">
          当前周期 <b>{cycleLabel(cycle.current_key)}</b> 已跑 {elapsed.toFixed(1)}h / {period}h（{pct}%）
          · 账本内完整周期 <b>{cycle.complete_cycles ?? 0}</b> 个
          {cycle.first_cycle_partial ? `（账本起点 ${cycleLabel(cycle.first_cycle_key)} 那个周期不完整，未计入）` : ''}
        </span>
      </div>

      {win?.empty_current_cycle && (
        <div className="review-param-empty">
          本周期刚在 {formatEnterClock(cycle.current_start_utc)} 开始，账本里还没有属于它的
          已平仓交易 —— 重置那一刻的强平算在上一个周期名下。
          <button type="button" className="linkish" onClick={() => onToggleWhole(true)}>
            看上一个完整周期
          </button>
        </div>
      )}

      {partial && (
        <div className="review-param-empty">
          ⚠ 窗口里含<b>进行中的当前周期</b>（只跑了 {elapsed.toFixed(1)}h，不足一个完整的 {period}h）。
          它的笔数与「日均」口径天然小于完整周期，做周期间对比时别直接放在一起平均。
          <button type="button" className="linkish" onClick={() => onToggleWhole(true)}>
            只算完整周期
          </button>
        </div>
      )}
      {!partial && wholeCycles && (
        <div className="review-param-empty">
          已排除进行中的当前周期，下方只统计跑满 {period}h 的周期。
          <button type="button" className="linkish" onClick={() => onToggleWhole(false)}>
            把当前周期也算上
          </button>
        </div>
      )}
    </div>
  );
}
