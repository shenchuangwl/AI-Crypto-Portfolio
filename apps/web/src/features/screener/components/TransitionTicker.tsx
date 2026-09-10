import type { TransitionEvent } from '../../../shared/types/screener';
import { STATE_LABEL } from '../../../shared/types/screener';
import { formatUtc } from '../../../shared/lib/format';

interface Props {
  events: TransitionEvent[];
}

export function TransitionTicker({ events }: Props) {
  if (!events.length) {
    return (
      <div className="transition-feed muted">
        状态变动区：本轮无预置变动事件
      </div>
    );
  }
  return (
    <div className="transition-feed">
      <div className="transition-title">状态变动区</div>
      <div className="transition-list">
        {events.map((e) => (
          <span key={e.id} className="transition-item">
            <time>{formatUtc(e.at_utc, 'HH:mm')}</time>
            <b>{e.symbol}</b>
            {STATE_LABEL[e.from_state]}→{STATE_LABEL[e.to_state]}
            {e.reason_codes.length > 0 && (
              <i>{e.reason_codes.join('/')}</i>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}
