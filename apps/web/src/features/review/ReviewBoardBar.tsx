import { BOARD_KEYS, BOARDS, type BoardKey } from '../../shared/config/boards';
import type { ReviewCoverage } from '../../shared/types/review';
import { formatEnterClock } from '../../shared/lib/format';

/**
 * 复盘「规则版本」切换条。
 *
 * 与下面的 `ParamVersionBar` 是**两层**，别混：
 *
 *   本条（board）        选哪一本账本 —— 即哪一套选币机制产出的交易
 *                        v1.4.0 = 选币榜 · v2.0.0 = 选币榜Y
 *   ParamVersionBar(pv)  在**同一本账本内**，只看某一段参数版本自己的成绩
 *
 * 两本账本是两个 sqlite 文件，从不合表：选入标准不同的交易混在一起算胜率没有意义
 * （§14.4），而且 v2.0.0 重放重建时也不该波及 v1.4.0 的历史。
 *
 * 现阶段 v2.0.0 是 TUNED_V2_MCAP_COMBO（权重分叉；216 天花板默认关）；
 * 两边数字分叉是预期，对照入口就是这一条。Y 的 DMR 仍不可执行。
 */
export function ReviewBoardBar({
  value,
  onChange,
  cov,
  error,
}: {
  value: BoardKey;
  onChange: (b: BoardKey) => void;
  cov?: ReviewCoverage;
  /** 当前账本取数失败时的原因（例如 Y 的账本还没铺底） */
  error?: string | null;
}) {
  const cfg = BOARDS[value];
  const notReady = Boolean(error) || cov?.ledger_ready === false;

  return (
    <div className={`review-banner board-bar${value !== 'main' ? ' filtered' : ''}`}>
      <div className="review-param-head">
        规则版本 <b>{cfg.rulesetLabel}</b> · {cfg.label}
        <span className="muted">
          {' '}
          {/* v1.3对齐：board只选账本，不隐式锁入点版本；X演进不得混用main历史或伪称迁移。 */}
          —— 当前账本为 <b>{cfg.dataDir}/review/ledger.sqlite</b>；
          统计范围由入点版本 / 参数指纹 / 时间窗口共同决定，各板面从不合并计算。
        </span>
        {cov?.watermark_scan_id && (
          <span className="muted">
            {' '}
            · 账本节点 {cov.watermark_scan_id}
            {cov.watermark_ts ? ` · ${formatEnterClock(cov.watermark_ts)}` : ''}
          </span>
        )}
      </div>

      {/* X仍复刻v1.4，旧dual-path需要独立内核/输入适配；版本消歧不是参数切换入口。 */}
      {value === 'x' && (
        <div className="muted">
          X 当前复刻 main v1.4.0；{cfg.parameterVersion} 不等于旧
          {' '}param-v1.3.0-dual-path-sticky。旧版历史请切选币榜账本，再选对应入点版本。
        </div>
      )}

      <div className="review-param-switch">
        {BOARD_KEYS.map((k) => {
          const b = BOARDS[k];
          return (
            <button
              key={k}
              type="button"
              className={`review-param-btn ${value === k ? 'on' : ''}`}
              onClick={() => onChange(k)}
              // X v1.3.0 复刻 main v1.4.0，复盘独立；未来 X overrides 不改变账本隔离。
              title={`${b.parameterVersion}\n实时榜：${b.route}\n账本：${b.dataDir}/review/ledger.sqlite`}
            >
              <span className="review-param-btn-name">{b.rulesetLabel}</span>
              <span className="review-param-btn-sub">{b.label}</span>
              <span className="review-param-btn-sub">{b.parameterVersion}</span>
            </button>
          );
        })}
      </div>

      {notReady && value !== 'main' && (
        <div className="review-param-empty">
          ⚠ <b>{cfg.rulesetLabel}</b> 的账本还没有数据。
          {cfg.label}的实时板面每 15 分钟随主扫描增量写入；要立刻铺底历史，跑一次
          <code>{` python3 scripts/replay_screener_y.py --board ${value} `}</code>
          —— 按 {cfg.parameterVersion} 重放共享历史指标，只写 {cfg.dataDir} 与独立 inbox。
        </div>
      )}
    </div>
  );
}
