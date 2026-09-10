import type { ColumnPreset } from '../../../shared/stores/screenerUi';
import type { LiquidityGrade } from '../../../shared/types/screener';

interface Props {
  search: string;
  onSearchChange: (q: string) => void;
  preset: ColumnPreset;
  onPresetChange: (p: ColumnPreset) => void;
  grades: LiquidityGrade[];
  onGradesChange: (g: LiquidityGrade[]) => void;
  visibleCount: number;
  totalCount: number;
  filterSummary: string;
  isDefaultFilter: boolean;
  onResetFilters: () => void;
}

const GRADES: LiquidityGrade[] = ['A', 'B', 'C', 'D'];

export function PoolToolbar({
  search,
  onSearchChange,
  preset,
  onPresetChange,
  grades,
  onGradesChange,
  visibleCount,
  totalCount,
  filterSummary,
  isDefaultFilter,
  onResetFilters,
}: Props) {
  const toggleGrade = (g: LiquidityGrade) => {
    if (grades.includes(g)) onGradesChange(grades.filter((x) => x !== g));
    else onGradesChange([...grades, g]);
  };

  return (
    <div className="toolbar">
      <input
        className="search"
        placeholder="搜索合约 / 标的…  (/)"
        value={search}
        onChange={(e) => onSearchChange(e.target.value)}
      />
      <label className="toolbar-label">
        列预设
        <select
          value={preset}
          onChange={(e) => onPresetChange(e.target.value as ColumnPreset)}
        >
          <option value="compact">精简</option>
          <option value="trader">交易员</option>
          <option value="full_s37">完整 §37</option>
        </select>
      </label>
      <div className="grade-filters">
        {GRADES.map((g) => (
          <button
            key={g}
            type="button"
            className={`grade-btn ${grades.includes(g) ? 'on' : ''}`}
            onClick={() => toggleGrade(g)}
          >
            {g}
          </button>
        ))}
        {grades.length > 0 && (
          <button type="button" className="linkish" onClick={() => onGradesChange([])}>
            清除
          </button>
        )}
      </div>
      <div className="toolbar-meta">
        显示 <b>{visibleCount}</b> / {totalCount}
      </div>
      <div className={`active-filter-summary ${isDefaultFilter ? '' : 'changed'}`}>
        当前筛选：<b>{filterSummary}</b>
        {!isDefaultFilter && (
          <button type="button" className="linkish" onClick={onResetFilters}>
            恢复默认
          </button>
        )}
      </div>
    </div>
  );
}
