import { useCallback, useEffect, useRef, useState } from 'react';
import { controlOnlyCoinSource, fetchOnlyCoinSource } from '../../shared/api/onlycoin';
import type { OnlyCoinSource } from '../../shared/types/onlycoin';
export function OnlyCoinSourceControl() {
  const [status, setStatus] = useState<OnlyCoinSource | null>(null);
  const [error, setError] = useState('');
  const [token, setToken] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const refresh = useCallback(async () => {
    try { const value = await fetchOnlyCoinSource(); if (alive.current) { setStatus(value); setError(''); } }
    catch (e) { if (alive.current) { setStatus(null); setError((e as Error).message); } }
  }, []);
  useEffect(() => { alive.current = true; void refresh(); const timer = window.setInterval(() => void refresh(), 15000); return () => { alive.current = false; clearInterval(timer); }; }, [refresh]);
  const change = async () => {
    if (!status || busy) return;
    setBusy(true); setError('');
    try {
      await controlOnlyCoinSource({ enabled: !status.enabled, expected_revision: status.revision, request_id: crypto.randomUUID(), reason: reason.trim() }, token);
      const verified = await fetchOnlyCoinSource(); // Never claim a PUT success without read-back.
      if (alive.current) setStatus(verified);
    } catch (e) { if (alive.current) { setStatus(null); setError((e as Error).message); } }
    finally { if (alive.current) { setBusy(false); setToken(''); } }
  };
  return <section className="onlycoin-source">
    <details><summary className="onlycoin-source-summary"><b>OnlyCoin 标的供应</b><span className="onlycoin-status">{status?.effective_state || '未知'}</span><span>仅资格，非交易开关</span><span className="onlycoin-manage">管理设置</span></summary><div className="onlycoin-source-body">
    <p>默认 OFF · 不授权下单，不改变现有持仓或其他来源。开启会供应当日已有成员。</p>
    <p>期望状态：{status ? status.enabled ? 'ON' : 'OFF' : '未知'} · 生效状态：{status?.effective_state || '未知'} · 消费者：{status ? status.consumer_connected ? '已连接' : '未连接' : '未知'} · revision {status?.revision ?? '—'} / generation {status?.generation ?? '—'}</p>
    {error && <p role="alert">来源不可用 / 操作未确认：{error}</p>}
    <div className="onlycoin-form"><label>管理员令牌（仅本组件内存）<input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} /></label>
    <label>操作原因<input value={reason} onChange={e => setReason(e.target.value)} /></label>
    <button type="button" disabled={!status || busy || !reason.trim() || status.eligibility_only !== true} onClick={() => void change()}>{busy ? '核验中…' : status?.enabled ? '关闭标的供应' : '开启标的供应'}</button>
    <button type="button" disabled={busy} onClick={() => void refresh()}>刷新来源状态</button>
    </div><small>需要服务端管理员授权；未配置或无权限时会拒绝。令牌不保存，提交后清除。</small>
    </div></details>
  </section>;
}
