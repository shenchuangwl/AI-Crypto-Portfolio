import { useCallback, useEffect, useMemo, useState } from 'react';
import { fetchScreenerLatest, subscribeScreenerEvents } from '../../shared/api/screener';
import type { CandidateRow, ScreenerSnapshot } from '../../shared/types/screener';
import { PaneErrorBoundary } from './PaneErrorBoundary';
import { TerminalStatusBar } from './TerminalStatusBar';
import { SelectionFunnel } from './SelectionFunnel';
import { BoardWall } from './BoardWall';
import { SelectionInspector } from './SelectionInspector';
import { funnelCounts } from './funnel';
import { rankBoard, type BoardId } from './boards';

/**
 * AICoin-style multi-board selection desk.
 * Not a clone of /screener — nine ranked boards + funnel + per-coin gate path.
 * Quotes live on the top-nav /quotes tab, not inside this desk.
 */
export function TerminalWorkspace() {
  const [snap, setSnap] = useState<ScreenerSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [board, setBoard] = useState<BoardId>('gainers');
  const [picked, setPicked] = useState<CandidateRow | null>(null);
  const snapId = snap?.meta.scan_id;

  const reload = useCallback(async () => {
    try {
      const s = await fetchScreenerLatest();
      setSnap(s);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!snapId) return;
    return subscribeScreenerEvents((ev) => {
      if (ev.type !== 'screener.updated' && ev.type !== 'confirmed.changed') return;
      const sid = String(ev.data.scan_id || '');
      if (sid && sid === snapId) return;
      void reload();
    });
  }, [!!snapId, reload, snapId]);

  const counts = useMemo(() => (snap ? funnelCounts(snap) : null), [snap]);

  useEffect(() => {
    if (!snap || picked) return;
    const first = rankBoard(snap, 'gainers', 1)[0] || rankBoard(snap, 'watch', 1)[0];
    if (first) setPicked(first);
  }, [snap, picked]);

  return (
    <div className="terminal-workspace aicoin-desk">
      <TerminalStatusBar />
      {error && <div className="desk-err">{error}</div>}
      {!snap || !counts ? (
        <div className="desk-loading">加载甄选快照…</div>
      ) : (
        <div className="desk-grid">
          <PaneErrorBoundary name="甄选漏斗">
            <SelectionFunnel
              counts={counts}
              scanId={snap.meta.scan_id}
              regime={snap.meta.regime_label}
            />
          </PaneErrorBoundary>
          <PaneErrorBoundary name="九榜墙">
            <BoardWall
              snap={snap}
              activeBoard={board}
              selected={picked?.symbol ?? null}
              onBoard={setBoard}
              onSelect={(row, id) => {
                setPicked(row);
                setBoard(id);
              }}
            />
          </PaneErrorBoundary>
          <PaneErrorBoundary name="甄选过程">
            <SelectionInspector row={picked} />
          </PaneErrorBoundary>
        </div>
      )}
    </div>
  );
}
