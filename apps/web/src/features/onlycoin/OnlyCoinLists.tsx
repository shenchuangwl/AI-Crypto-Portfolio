import { Link } from 'react-router-dom';
import { formatEnterClock } from '../../shared/lib/format';
import type { OnlyCoinDaily } from '../../shared/types/onlycoin';
export function OnlyCoinLists({ data, originTag = 'screener-y' }: { data: OnlyCoinDaily; originTag?: 'screener-y' | 'review-onlycoin' }) {
  return <>
    <p className="muted">业务日 {data.business_date}（00:00 UTC 起算）· 时间显示 UTC+7 · scan {data.as_of_scan_id || '—'} · revision {data.projection_revision} {data.stale ? ' · 数据陈旧' : ''}</p>
    <div className="onlycoin-lists">{(['long', 'short', 'onlycoin'] as const).map(key => <div key={key}>
      <h3><span>{key === 'onlycoin' ? 'OnlyCoin' : key.toUpperCase()} ({data.counts[key] ?? data[key].length})</span>{data[key].length > 6 && <small>滚动查看全部</small>}</h3>
      <ul>{data[key].map(member => <li key={member.member_id}>
        <Link to={`/market/${encodeURIComponent(member.symbol)}?from=${originTag}&direction=${member.first_direction === 'SHORT' || member.first_direction === 'down' ? 'down' : 'up'}`}>{member.symbol}</Link>
        <span className="onlycoin-time" title={`首次入选 UTC ${member.first_selected_at}；首次可用 ${member.first_available_at || '未知'}；scan ${member.first_scan_id}`}>{formatEnterClock(member.first_selected_at)} <small>+07</small></span>
        <details><summary title={`${member.directions.join('/')} · 查看入选证据`}>证据</summary><pre>{JSON.stringify({ member_id: member.member_id, directions: member.directions, first_scan_id: member.first_scan_id, first_available_at: member.first_available_at, parameter_version: member.parameter_version, rule_identity: member.rule_identity, provenance: member.provenance }, null, 2)}</pre></details>
      </li>)}{!data[key].length && <li className="muted">暂无成员</li>}</ul>
    </div>)}</div>
    <details className="onlycoin-coverage"><summary>覆盖度 / 来源证据（不代表完整历史）</summary><pre>{JSON.stringify(data.coverage, null, 2)}</pre></details>
  </>;
}
