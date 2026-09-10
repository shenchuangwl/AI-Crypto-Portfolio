import { formatEnterClock } from '../../shared/lib/format';
import { REVIEW_ZONE_LABEL } from '../../shared/types/review';
import type { ReviewCoverage, ReviewZone } from '../../shared/types/review';

/**
 * 覆盖度 / 新鲜度 / 参数版本三类告警（§14.3、§14.4）。
 *
 * 硬性规则：请求周期长于实际覆盖，或所选分区的可用起点晚于周期起点时，**必须**
 * 出现醒目告警。禁止返回一个看起来完整、实则被数据长度截断的数字。
 */
export function CoverageBanner({ cov, zones }: { cov?: ReviewCoverage; zones: ReviewZone[] }) {
  if (!cov) return null;
  const requested = cov.requested_days;
  const available = cov.available_days;
  const shortHist = available != null && requested != null && available + 1e-6 < requested;
  const shortZones = (cov.short_zones || []).filter((z) => zones.includes(z));
  const fresh = cov.freshness;
  // 参数版本告警与切换由 ParamVersionBar 独占，这里不重复渲染。
  if (!shortHist && !shortZones.length && !fresh?.stale) return null;

  return (
    <div className="review-banners">
      {fresh?.stale && (
        <div className="review-banner stale">
          ⚠ 账本落后于最新扫描节点：账本 <b>{cov.watermark_scan_id || '—'}</b>
          {' → '}现网 <b>{fresh.latest_scan_id}</b>
          {fresh.lag_nodes != null ? `（落后 ${fresh.lag_nodes} 个节点 / ${Math.round(fresh.lag_minutes || 0)} 分钟）` : ''}
          。下方数字截至账本节点。
          {fresh.hint ? <span className="muted"> {fresh.hint}</span> : null}
        </div>
      )}

      {(shortHist || shortZones.length > 0) && (
        <div className="review-banner warn">
          {shortHist && (
            <span>
              ⚠ 快照覆盖 <b>{available?.toFixed(2)}</b> / {Number(requested).toFixed(0)} 天
              {cov.first_ts ? `（自 ${formatEnterClock(cov.first_ts)} 本地）` : ''}
              ——「近 {Number(requested).toFixed(0)} 天」当前物理上取不满，缺的不是交易，是历史。
            </span>
          )}
          {shortZones.map((z) => {
            const zc = cov.zone_available?.[z];
            return (
              <span key={z} className="review-banner-item">
                {' '}· <b>{REVIEW_ZONE_LABEL[z]}</b> 区自 {zc?.from ? formatEnterClock(zc.from) : '—'} 起，
                实际 {zc?.days?.toFixed(2) ?? '—'} / {Number(requested).toFixed(0)} 天
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
