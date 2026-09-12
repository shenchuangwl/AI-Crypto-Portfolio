import type { BoardConfig } from '../config/boards';
import type { ScanMeta } from '../types/screener';

/**
 * 216 天花板的**当前**状态，只认快照，不认页头写死的历史说明。
 *
 * 优先级：`meta.mcap_effective.mode`（off/shadow/on）>
 * `rule_identity.feature_flags.ENABLE_MCAP_ZONE`。
 * 缺字段时返回 null，调用方回退静态 note。
 */
export function mcapCeilingLabel(meta?: ScanMeta | null): string | null {
  const block = meta?.mcap_effective;
  const mode = String(
    block && typeof block === 'object' && 'mode' in block ? (block as { mode?: unknown }).mode : '',
  ).toLowerCase();
  if (mode === 'on') return '216 天花板 开（只降不升，正在裁决）';
  if (mode === 'shadow') return '216 天花板 影子（只观察，不改分区）';
  if (mode === 'off') return '216 天花板 关（分区只看状态机）';
  const flags = meta?.rule_identity?.feature_flags;
  const on =
    flags && typeof flags === 'object' && 'ENABLE_MCAP_ZONE' in flags
      ? Boolean((flags as { ENABLE_MCAP_ZONE?: unknown }).ENABLE_MCAP_ZONE)
      : undefined;
  if (on === true) return '216 天花板 开';
  if (on === false) return '216 天花板 关';
  return null;
}

/** 页头副标题：保留权重/不可执行等静态句，用快照替换过期的「默认关」。 */
export function screenerBrandNote(cfg: BoardConfig, meta?: ScanMeta | null): string | undefined {
  const live = mcapCeilingLabel(meta);
  const raw = cfg.note || '';
  if (!live) return raw || undefined;
  const base = raw
    .replace(/216 天花板默认关；?/g, '')
    .replace(/216\/Z\/K\/天花板仅 shadow 观察；?/g, '')
    .replace(/；+$/g, '');
  return [base, live].filter(Boolean).join('；');
}
