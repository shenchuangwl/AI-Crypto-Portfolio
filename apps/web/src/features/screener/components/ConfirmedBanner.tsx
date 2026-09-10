import type { CandidateRow } from '../../../shared/types/screener';
import { Link } from 'react-router-dom';
import { BOARDS, DEFAULT_BOARD, originTagFor, type BoardKey } from '../../../shared/config/boards';

/** Rows visible without scrolling; must stay in sync with `--conf-rows` in app.css. */
const VISIBLE_ROWS = 6;

interface Props {
  /** X v1.3.0 复刻 main v1.4.0，但不可执行且复盘独立；未来只经 X overrides 演进。 */
  board?: BoardKey;
  long: CandidateRow[];
  short: CandidateRow[];
  scanId?: string;
  pulse?: boolean;
  onRefresh?: () => void;
  lastEvent?: string;
}

export function ConfirmedBanner({
  board = DEFAULT_BOARD,
  long,
  short,
  scanId,
  pulse,
  onRefresh,
  lastEvent,
}: Props) {
  const count = long.length + short.length;
  return (
    <section
      className={`confirmed-banner${pulse ? ' pulse' : ''}${count ? ' has-items' : ''}`}
      aria-live="polite"
    >
      <div className="confirmed-banner-head">
        <div>
          <span className="confirmed-kicker">DMR区 · 精选执行</span>
          <h2>
            {count ? (
              <>
                <strong>{count}</strong> DMR精选标的
              </>
            ) : (
              <>暂无DMR精选（等待严格执行门槛）</>
            )}
          </h2>
          <p className="muted">
            scan {scanId || '—'}
            {lastEvent ? ` · ${lastEvent}` : ''}
            {count
              ? (BOARDS[board].dmrExecutable ? ' · 已写入 dmr-adapter inbox，可 paper 执行'
                : ' · 已写入独立 inbox，仅观察与复盘，不可执行')
              : ' · 生产路径无 SM_FAST，需多轮 15m'}
          </p>
        </div>
        <button type="button" className="btn-ghost" onClick={onRefresh}>
          刷新
        </button>
      </div>
      {count > 0 && (
        <div className="confirmed-lists">
          <div>
            <h3>
              <span>LONG ({long.length})</span>
              {long.length > VISIBLE_ROWS && (
                <span className="more-hint">滚动查看全部 {long.length}</span>
              )}
            </h3>
            <ul>
              {long.map((r) => (
                <li key={`L-${r.symbol}`}>
                  {/* X v1.3.0 DMR 横幅也须往返正确；main 原 DOM 保留，复盘/演进不另造页面。 */}
                  {board === 'main' ? <span className="sym">{r.symbol}</span> :
                    <Link className="sym" to={`/market/${r.symbol}?direction=up&from=${originTagFor(board)}`}>{r.symbol}</Link>}
                  <span className="score">{r.score_up?.toFixed?.(1) ?? r.score_up}</span>
                  <span className="ss">SS {r.staircase_score?.toFixed?.(0)}</span>
                </li>
              ))}
              {!long.length && <li className="muted">—</li>}
            </ul>
          </div>
          <div>
            <h3>
              <span>SHORT ({short.length})</span>
              {short.length > VISIBLE_ROWS && (
                <span className="more-hint">滚动查看全部 {short.length}</span>
              )}
            </h3>
            <ul>
              {short.map((r) => (
                <li key={`S-${r.symbol}`}>
                  {board === 'main' ? <span className="sym">{r.symbol}</span> :
                    <Link className="sym" to={`/market/${r.symbol}?direction=down&from=${originTagFor(board)}`}>{r.symbol}</Link>}
                  <span className="score">
                    {r.score_down?.toFixed?.(1) ?? r.score_down}
                  </span>
                  <span className="ss">SS {r.staircase_score?.toFixed?.(0)}</span>
                </li>
              ))}
              {!short.length && <li className="muted">—</li>}
            </ul>
          </div>
        </div>
      )}
    </section>
  );
}
