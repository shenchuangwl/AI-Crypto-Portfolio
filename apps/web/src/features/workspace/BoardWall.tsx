import { useMemo } from 'react';
import type { CandidateRow, ScreenerSnapshot } from '../../shared/types/screener';
import { STATE_LABEL } from '../../shared/types/screener';
import { useTickerStore } from '../../shared/stores/tickerStore';
import { formatNum } from '../../shared/lib/format';
import { BOARDS, BOARD_LIMIT, boardMetric, rankBoard, type BoardId } from './boards';

interface Props {
  snap: ScreenerSnapshot;
  activeBoard: BoardId;
  selected: string | null;
  onSelect: (row: CandidateRow, board: BoardId) => void;
  onBoard: (id: BoardId) => void;
}

function LastPx({ symbol }: { symbol: string }) {
  const q = useTickerStore((s) => s.quotes[symbol]);
  const v = q?.last ?? q?.mark;
  if (v == null) return <span className="muted">—</span>;
  return <span className="live-last">{formatNum(v, v >= 100 ? 1 : 5)}</span>;
}

export function BoardWall({ snap, activeBoard, selected, onSelect, onBoard }: Props) {
  const lists = useMemo(
    () => BOARDS.map((b) => ({ def: b, rows: rankBoard(snap, b.id, BOARD_LIMIT) })),
    [snap],
  );

  return (
    <div className="board-wall">
      {lists.map(({ def, rows }) => (
        <section
          key={def.id}
          className={`mini-board${activeBoard === def.id ? ' on' : ''}`}
          onClick={() => onBoard(def.id)}
        >
          <header>
            <b>{def.title}</b>
            <em>{rows.length}</em>
          </header>
          <p className="mini-hint">{def.hint}</p>
          <ol>
            {rows.length === 0 && !['dmr', 'confirmed', 'qualified'].includes(def.id) && (
              <li className="mini-empty">本榜暂无</li>
            )}
            {rows.map((r, i) => {
              const m = boardMetric(r, def.id);
              return (
                <li key={`${def.id}:${r.direction}:${r.symbol}`}>
                  <button
                    type="button"
                    className={`mini-row${selected === r.symbol ? ' sel' : ''}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect(r, def.id);
                    }}
                  >
                    <span className="rk">{i + 1}</span>
                    <span className="sym">
                      {r.symbol.replace(/USDT$/, '')}
                      {(['dmr', 'confirmed', 'qualified'] as BoardId[]).includes(def.id) && (
                        <i className={r.direction === 'down' ? 'dir-dn' : 'dir-up'}>
                          {r.direction === 'down' ? '↓' : '↑'}
                        </i>
                      )}
                    </span>
                    <span className={`st st-${r.state.toLowerCase()}`}>{STATE_LABEL[r.state]}</span>
                    <LastPx symbol={r.symbol} />
                    <span className={`met ${m.cls || ''}`}>{m.value}</span>
                  </button>
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}
