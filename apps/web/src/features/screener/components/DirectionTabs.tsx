import type { Direction } from '../../../shared/types/screener';

interface Props {
  value: Direction;
  /** 当前分区组合下，上涨池里的**唯一币种数**（已去重，不是条目数）。 */
  longCount: number;
  shortCount: number;
  /** 当前选中的分区，用于 tooltip 说明这个数字是怎么来的。 */
  zoneSummary?: string;
  onChange: (d: Direction) => void;
}

/**
 * 两个候选池的切换标签。数字随分区多选实时变化 —— 选中分区取并集后按唯一币种 ID
 * 去重，同一币种落在多个分区里只算一次（DMR 是确认的子集，最容易被重复累加）。
 */
export function DirectionTabs({ value, longCount, shortCount, zoneSummary, onChange }: Props) {
  const tip = (pool: string, n: number) =>
    `${pool}：${zoneSummary ? `${zoneSummary} 并集去重后` : '当前分区并集去重后'}的唯一币种数 ${n}`;
  return (
    <div className="dir-tabs">
      <button
        type="button"
        className={`dir-tab up ${value === 'up' ? 'on' : ''}`}
        title={tip('上涨候选池', longCount)}
        onClick={() => onChange('up')}
      >
        上涨候选池 <span>{longCount}</span>
      </button>
      <button
        type="button"
        className={`dir-tab down ${value === 'down' ? 'on' : ''}`}
        title={tip('下跌候选池', shortCount)}
        onClick={() => onChange('down')}
      >
        下跌候选池 <span>{shortCount}</span>
      </button>
    </div>
  );
}
