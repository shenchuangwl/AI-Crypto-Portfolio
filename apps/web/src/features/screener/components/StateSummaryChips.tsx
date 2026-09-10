import type { ScreenerState } from '../../../shared/types/screener';
import { STATE_LABEL } from '../../../shared/types/screener';

const ORDER: ScreenerState[] = [
  'CONFIRMED',
  'QUALIFIED',
  'WATCH',
  'ELIMINATED',
  'DATA_INSUFFICIENT',
  'LOW_CONFIDENCE',
];

interface Props {
  counts: Partial<Record<ScreenerState, number>>;
  dmrCount: number;
  dmrActive: boolean;
  onToggleDmr: () => void;
  active: ScreenerState[];
  onToggle: (s: ScreenerState) => void;
}

export function StateSummaryChips({
  counts,
  dmrCount,
  dmrActive,
  onToggleDmr,
  active,
  onToggle,
}: Props) {
  return (
    <div className="chips">
      <button
        type="button"
        className={`chip state-dmr ${dmrActive ? 'on' : ''}`}
        onClick={onToggleDmr}
        title="真实执行 inbox；可与确认、符合或其它状态多选"
      >
        <span className="chip-dot" />
        DMR
        <b>{dmrCount}</b>
      </button>
      {ORDER.map((s) => {
        const n = counts[s] ?? 0;
        const on = active.includes(s);
        return (
          <button
            key={s}
            type="button"
            className={`chip state-${s.toLowerCase()} ${on ? 'on' : ''}`}
            onClick={() => onToggle(s)}
          >
            <span className="chip-dot" />
            {STATE_LABEL[s]}
            <b>{n}</b>
          </button>
        );
      })}
    </div>
  );
}
