import type { BoardKey } from '../config/boards';
import type { ReviewPeriod, ReviewRange } from '../stores/reviewUi';
import { defaultReviewRange } from './reviewTime';

/** Only time controls are remembered; trading filters and parameter locks stay in the URL. */
export interface ReviewTimePreferences {
  rangeDays: ReviewRange;
  customFrom: string;
  customTo: string;
  useCustom: boolean;
  comparePeriods: ReviewPeriod[];
  compareOn: boolean;
}

export const REVIEW_TIME_STORAGE_KEY = 'review-time-v1';

export function readReviewTime(board: BoardKey): ReviewTimePreferences {
  const range = defaultReviewRange();
  const fallback: ReviewTimePreferences = {
    rangeDays: 7, customFrom: range.from, customTo: range.to,
    useCustom: false, comparePeriods: [], compareOn: false,
  };
  try {
    const raw = globalThis.localStorage?.getItem(`${REVIEW_TIME_STORAGE_KEY}:${board}`);
    if (!raw) return fallback;
    const s = JSON.parse(raw);
    const validTime = (v: unknown) => typeof v === 'string' &&
      (v === '' || (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/.test(v) && Number.isFinite(Date.parse(v))));
    if (!s || ![1, 7, 14, 28].includes(s.rangeDays) ||
        !validTime(s.customFrom) || !validTime(s.customTo) ||
        typeof s.useCustom !== 'boolean' || typeof s.compareOn !== 'boolean' ||
        !Array.isArray(s.comparePeriods) || s.comparePeriods.length > 3 ||
        !s.comparePeriods.every((p: ReviewPeriod) => p && typeof p.id === 'string' && validTime(p.from) && validTime(p.to) &&
          (p.label === undefined || typeof p.label === 'string')) ||
        new Set(s.comparePeriods.map((p: ReviewPeriod) => p.id)).size !== s.comparePeriods.length) return fallback;
    return {
      rangeDays: s.rangeDays, customFrom: s.customFrom, customTo: s.customTo,
      useCustom: s.useCustom && Boolean(s.customFrom && s.customTo),
      compareOn: s.compareOn,
      comparePeriods: s.comparePeriods.map((p: ReviewPeriod) => ({ id: p.id, from: p.from, to: p.to, label: p.label })),
    };
  } catch {
    // Storage can be disabled or corrupted. Never block the review page.
    return fallback;
  }
}

export function writeReviewTime(board: BoardKey, s: ReviewTimePreferences): void {
  try {
    globalThis.localStorage?.setItem(`${REVIEW_TIME_STORAGE_KEY}:${board}`, JSON.stringify({
      rangeDays: s.rangeDays, customFrom: s.customFrom, customTo: s.customTo,
      useCustom: s.useCustom, comparePeriods: s.comparePeriods, compareOn: s.compareOn,
    }));
  } catch {
    // In-memory edits still work when storage is unavailable or full.
  }
}
