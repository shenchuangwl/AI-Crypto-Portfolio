import type { ReviewTrade } from '../types/review';

/**
 * 复盘明细导出。字段与账本一一对应（含 `flags`），供离线核对与二次分析。
 * 盈亏列导出**原始小数**（0.0231），不导出展示用的四舍五入字符串——
 * 用展示值回算是本项目明令禁止的做法。
 */
export const CSV_COLUMNS: Array<keyof ReviewTrade> = [
  'trade_id',
  'symbol',
  'canonical_asset_id',
  'direction',
  'zone',
  'status',
  'enter_scan_id',
  'enter_time_utc',
  'exit_scan_id',
  'exit_time_utc',
  'dwell_minutes',
  'dwell_nodes',
  'enter_price',
  'enter_price_source',
  'exit_price',
  'exit_price_source',
  'pnl_pct',
  'pnl_sign',
  'score',
  'score_opposite',
  'direction_confidence',
  'liquidity_grade',
  'mcap_grade_30m',
  'mcap_grade_2h',
  'mcap_grade_6h',
  'ret_1h',
  'ret_4h',
  'ret_24h',
  'ret_1w',
  'ret_1mo',
  'ret_since_anchor',
  'risk_flags',
  'reason_codes',
  'qualified_path',
  'confirmed_path',
  'parameter_version',
  'flags',
];

function cell(v: unknown): string {
  if (v == null) return '';
  const s = Array.isArray(v) ? v.join('|') : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function tradesToCsv(rows: ReviewTrade[]): string {
  const head = CSV_COLUMNS.join(',');
  const body = rows.map((t) => CSV_COLUMNS.map((c) => cell(t[c])).join(','));
  // BOM so Excel opens the Chinese reason codes as UTF-8.
  return `﻿${[head, ...body].join('\n')}\n`;
}

export function downloadCsv(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
