import type { FunnelCounts } from './funnel';

interface Props {
  counts: FunnelCounts;
  scanId: string;
  regime?: string;
}

const STEPS: { key: keyof FunnelCounts; label: string; hint: string }[] = [
  { key: 'universe', label: '有效宇宙', hint: 'USDT+PERP+COIN' },
  { key: 'g1', label: 'G1 流动性过线', hint: '6/12/26 日成交额' },
  { key: 'g3', label: 'G3 有流通量', hint: 'CoinGecko 供应' },
  { key: 'watch', label: '观察区 WATCH', hint: '连续扫描滞回' },
  { key: 'dmr', label: 'DMR区 精选', hint: '真实执行标的' },
  { key: 'confirmed', label: '确认区 CONF', hint: '旋转楼梯硬标准' },
  { key: 'qualified', label: '符合区 QUAL', hint: '含确认与DMR' },
];

export function SelectionFunnel({ counts, scanId, regime }: Props) {
  const max = Math.max(counts.universe, 1);
  return (
    <aside className="sel-funnel">
      <header className="sel-col-head">
        <b>甄选漏斗</b>
        <span>{scanId}</span>
      </header>
      <ol className="funnel-list">
        {STEPS.map((s, i) => {
          const n = counts[s.key];
          const pct = Math.max(2, Math.round((n / max) * 100));
          return (
            <li key={s.key} className={`funnel-step fs-${s.key}`}>
              <div className="funnel-meta">
                <span className="funnel-idx">{i + 1}</span>
                <span className="funnel-label">{s.label}</span>
                <span className="funnel-n">{n}</span>
              </div>
              <div className="funnel-bar" aria-hidden>
                <i style={{ width: `${pct}%` }} />
              </div>
              <div className="funnel-hint">{s.hint}</div>
            </li>
          );
        })}
      </ol>
      <div className="funnel-aside">
        <span>淘汰 {counts.eliminated}</span>
        <span>数据不足 {counts.insufficient}</span>
        <span>{regime || 'g1_g2_g3_g4_sm'}</span>
      </div>
    </aside>
  );
}
