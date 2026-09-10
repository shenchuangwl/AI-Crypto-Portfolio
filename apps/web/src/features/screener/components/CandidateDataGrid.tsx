import { useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useNavigate } from 'react-router-dom';
import type { CandidateRow } from '../../../shared/types/screener';
import type { ColumnPreset, SortId } from '../../../shared/stores/screenerUi';
import { buildCols } from './candidateColumns';

interface Props {
  rows: CandidateRow[];
  preset: ColumnPreset;
  direction: 'up' | 'down';
  /**
   * 排序状态由页面传入，而不是在这里直接读某个 store。
   * 「选币榜」与「选币榜Y」各有一份 UI 仓库；组件写死单例就会让两块板面共用排序。
   */
  sortId: SortId;
  sortDesc: boolean;
  onSort: (id: SortId) => void;
  /** 点击行跳 K 线时带回的来源标记（screener / screener-y） */
  // X v1.3.0 与 main v1.4.0 共用表格但来源必填；复盘/往返独立，演进只经 X overrides。
  originTag: string;
}

export function CandidateDataGrid({
  rows,
  preset,
  direction,
  sortId,
  sortDesc,
  onSort,
  originTag,
}: Props) {
  const parentRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const cols = buildCols(preset, direction);

  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 40,
    overscan: 12,
  });

  const totalWidth = cols.reduce((a, c) => a + c.width, 0);

  return (
    <div className="grid-wrap" ref={parentRef}>
      <div className="grid-header" style={{ width: totalWidth }}>
        {cols.map((c) => (
          <div
            key={c.id}
            className={`th ${c.sortKey ? 'sortable' : ''} ${sortId === c.sortKey ? 'sorted' : ''}`}
            style={{ width: c.width }}
            onClick={() => c.sortKey && onSort(c.sortKey)}
          >
            {c.label}
            {c.sortKey && sortId === c.sortKey ? (sortDesc ? ' ↓' : ' ↑') : ''}
          </div>
        ))}
      </div>
      <div
        className="grid-body"
        style={{ height: rowVirtualizer.getTotalSize(), width: totalWidth }}
      >
        {rowVirtualizer.getVirtualItems().map((vr) => {
          const r = rows[vr.index];
          return (
            <div
              key={`${r.direction}:${r.symbol}`}
              className={`grid-row state-row-${r.state.toLowerCase()}`}
              style={{
                height: vr.size,
                transform: `translateY(${vr.start}px)`,
                width: totalWidth,
              }}
              onClick={() =>
                navigate(
                  `/market/${r.symbol}?direction=${direction}&from=${originTag}`,
                )
              }
            >
              {cols.map((c) => (
                <div key={c.id} className="td" style={{ width: c.width }}>
                  {c.render(r)}
                </div>
              ))}
            </div>
          );
        })}
      </div>
      {rows.length === 0 && <div className="empty">当前过滤条件下无候选</div>}
    </div>
  );
}
