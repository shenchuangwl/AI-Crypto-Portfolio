import { useEffect, useState } from 'react';
import type { ScanCycle } from '../../../shared/types/screener';
import { formatEnterClock } from '../../../shared/lib/format';

/** 秒 → `4h37m` / `12m08s`。倒计时用，短于 1 小时才显示秒。 */
function countdown(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h${String(m).padStart(2, '0')}m`;
  return `${m}m${String(s).padStart(2, '0')}s`;
}

/**
 * 时间区（24 小时周期）状态条 —— 只有启用了周期管理的板面才出现。
 *
 * 「选币榜」没有周期，分区成员跨天连续持有，`meta.cycle` 根本不存在，这条不渲染。
 * 「选币榜Y」是 24h 循环：周期节点一到清空全部分区，再由同一节点的实时扫描从零重建。
 *
 * 这条要回答三个问题，缺一个用户就会以为板面坏了：
 *   1. 现在是哪个周期、走到第几个节点、离下次重置还有多久
 *   2. 刚刚是不是重置了（重置节点上确认区必然是空的）
 *   3. 分区是不是还在重建（周期头 75 分钟没有确认区是设计，不是数据没到）
 */
export function CycleStrip({ cycle }: { cycle: ScanCycle }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (!cycle?.enabled) return null;

  const node = cycle.node_in_cycle ?? 0;
  const total = cycle.nodes_per_cycle ?? 96;
  const endMs = cycle.cycle_end_utc ? Date.parse(cycle.cycle_end_utc) : NaN;
  const toReset = Number.isFinite(endMs) ? (endMs - now) / 1000 : NaN;
  const pct = total > 0 ? Math.min(100, Math.max(0, ((node + 1) / total) * 100)) : 0;

  // 重建窗口：清空后状态机从 NONE 起步，v1.4.0 的时间门槛下最快第 5 个节点
  // （75 分钟）才会出现确认区。warmup 打开时按它自己的节点数算。
  const warmupNodes = cycle.warmup?.enabled ? cycle.warmup.nodes ?? 0 : 0;
  const rebuildNodes = 5;
  const effectiveFrom = cycle.last_reset_node_in_cycle ?? 0;
  const sinceReset = node - effectiveFrom;
  const rebuilding = sinceReset < rebuildNodes;
  const closed = cycle.closed_zones
    ? Object.entries(cycle.closed_zones).filter(([k, v]) => k !== 'NONE' && v > 0)
    : [];

  return (
    <div className={`cycle-strip${cycle.reset_at_this_node ? ' reset' : ''}`}>
      <div className="cycle-row">
        <span className="cycle-badge">24h 周期</span>
        <span className="cycle-main">
          本周期 <b>{cycle.cycle_key ?? '—'}</b>
          {cycle.cycle_start_utc ? (
            <span className="muted">
              {' '}
              · 起点 {formatEnterClock(cycle.cycle_start_utc)}
              {cycle.period_hours ? ` · 每 ${cycle.period_hours}h 重置一次` : ''}
            </span>
          ) : null}
        </span>
        <span className="cycle-node">
          第 <b>{node}</b> / {total} 节点
        </span>
        <span className="cycle-eta">
          距下次清空重建 <b>{countdown(toReset)}</b>
        </span>
      </div>

      <div className="cycle-bar" aria-hidden>
        <span className="cycle-bar-fill" style={{ width: `${pct}%` }} />
      </div>

      {cycle.reset_at_this_node && (
        <div className="cycle-note reset">
          ⟳ 本节点已执行周期重置：全部分区的历史币种数据已清空
          {closed.length ? (
            <span className="muted">
              {' '}
              （清出 {closed.map(([k, v]) => `${k} ${v}`).join(' · ')}）
            </span>
          ) : null}
          ，正在按新周期起点重新采集并重建分区。
          <span className="muted">
            {' '}
            本节点表内只会看到当刻就能直接判定的淘汰区 / 数据不足区；
            观察区从下一节点起填充，确认区 / DMR 区约第 5 个节点（75 分钟）出现。
          </span>
        </div>
      )}

      {!cycle.reset_at_this_node && rebuilding && (
        <div className="cycle-note warm">
          分区重建中（本周期第 {sinceReset} 个节点）—— 清空后状态机从零起步，
          受停留与连击门槛约束
          {warmupNodes > 0
            ? `（重建加速已开启：前 ${warmupNodes} 个节点）`
            : '：观察区第 1 节点、符合区第 3 节点、确认区 / DMR 区第 5 节点（约 75 分钟）'}
          。在此之前这几个区为空是<b>预期行为</b>，不是数据没到。
          <span className="muted">
            {' '}
            板面按分区呈现，所以还没进入任何分区的币暂时不在表内；G1–G4 的行情、
            涨跌幅与周期市值等级当刻已全部算好，币一进观察区就带着完整数据出现。
          </span>
        </div>
      )}

      {cycle.partial_cycle && (
        <div className="cycle-note warn">
          ⚠ 本周期的分区不是从 0 号节点开始重建的
          {cycle.last_reset_at_utc ? `（自 ${formatEnterClock(cycle.last_reset_at_utc)} 起）` : ''}
          ：
          {cycle.partial_reason === 'cold_start'
            ? '这是首次启用周期管理。'
            : '扫描循环停机跨过了周期节点。'}
          本周期的统计口径因此不足一个完整周期，下个周期起恢复正常。
        </div>
      )}
    </div>
  );
}
