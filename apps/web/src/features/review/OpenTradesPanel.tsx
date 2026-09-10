import { useState } from 'react';
import type { ReviewTrade } from '../../shared/types/review';
import { ReviewDataGrid } from './ReviewDataGrid';

/**
 * 未平仓折叠区（§12.2）。
 *
 * R1：这些记录**不进** `trades`、不进合计/平均/胜率。放在主表下方而不是混进
 * 主表，是为了让「真实交易次数」这个主数字永远只数已完成的进出。这里的浮动
 * 盈亏列用实时价算，明确标注为未实现，仅供参考。
 */
export function OpenTradesPanel({ rows, total }: { rows: ReviewTrade[]; total: number }) {
  const [open, setOpen] = useState(false);
  if (!total) return null;
  return (
    <div className="review-open-panel">
      <button type="button" className="review-open-head" onClick={() => setOpen((v) => !v)}>
        {open ? '▾' : '▸'} 未平仓 <b>{total}</b> 笔
        <span className="muted">
          （不计入上方任何盈亏统计 · 浮动盈亏为未实现参考值{rows.length < total ? ` · 本页 ${rows.length} 笔` : ''}）
        </span>
      </button>
      {open && <ReviewDataGrid rows={rows} columnPreset="compact" height={320} />}
    </div>
  );
}
