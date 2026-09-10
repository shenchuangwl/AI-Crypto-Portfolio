"""Per-anchor-day unique-symbol ledger (计划 §1.3 三个数量口径).

The board headline counts are **瞬时占用** (this scan) and **侧** (symbol×direction).
Neither answers "今天一共有多少币进过确认区" — that is **日去重入选**, and 计划
§13.4 requires the two to be shown together so nobody reads a day roll-up as if it
were the DMR-consumable set of one 15-minute node.

The ledger is an append-only set per UTC anchor day, reset when the anchor rolls.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("coin_selection.daily_ledger")

TRACKED_STATES = ("WATCH", "QUALIFIED", "CONFIRMED", "ELIMINATED", "DMR")

# DMR is not a state-machine state: it is the post-quality, same-symbol-deduped,
# Top-K selected set. Keeping it in the same per-day ledger makes its "今日去重"
# explicit without confusing it with CONFIRMED day roll-up.


class DailyUniqueLedger:
    """Union of symbols that entered each zone at least once on one anchor day."""

    def __init__(self, path: Path, anchor_date: str):
        self.path = path
        self.anchor_date = anchor_date
        self.states: dict[str, set[str]] = {s: set() for s in TRACKED_STATES}
        # Old day-ledger files predate the DMR field. Seed once from today's
        # existing inbox batches so "今日去重" means the whole UTC cycle, not
        # merely scans after this feature was deployed.
        self.dmr_known = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # corrupt file must never break a scan (v1.2 P9)
            log.warning("daily ledger load failed: %s", e)
            return
        if str(obj.get("anchor_date") or "") != self.anchor_date:
            return  # anchor rolled → fresh day, old file is superseded
        for st, syms in (obj.get("states") or {}).items():
            if st in self.states and isinstance(syms, list):
                self.states[st] = {str(x) for x in syms}
                if st == "DMR":
                    self.dmr_known = True

    def observe(self, rows: Iterable[dict[str, Any]]) -> None:
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            for key in ("state_up", "state_down"):
                st = r.get(key)
                if st in self.states:
                    self.states[st].add(str(sym))

    def observe_dmr(self, messages: Iterable[dict[str, Any]]) -> None:
        """Record only actual DMR inbox selections after dedupe and Top-K."""
        for message in messages:
            sym = message.get("symbol")
            if sym:
                self.states["DMR"].add(str(sym))
        self.dmr_known = True

    def seed_dmr_from_inbox(self, inbox_dir: Path) -> None:
        """One-time same-cycle backfill from already-written DMR inbox batches."""
        if self.dmr_known or not inbox_dir.is_dir():
            return
        prefix = self.anchor_date.replace("-", "") + "-"
        for path in sorted(inbox_dir.glob(f"{prefix}*.candidates.json")):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                self.observe_dmr(body.get("candidates") or ())
            except Exception as e:  # a damaged historical inbox must not stop the scan
                log.warning("daily DMR inbox backfill skipped %s: %s", path.name, e)
        self.dmr_known = True

    def counts(self) -> dict[str, Any]:
        return {
            "anchor_date": self.anchor_date,
            "confirmed": len(self.states["CONFIRMED"]),
            "qualified": len(self.states["QUALIFIED"]),
            "watch": len(self.states["WATCH"]),
            "eliminated": len(self.states["ELIMINATED"]),
            "dmr": len(self.states["DMR"]),
            "confirmed_symbols": sorted(self.states["CONFIRMED"]),
            "dmr_symbols": sorted(self.states["DMR"]),
            "note": "confirmed/qualified/watch/eliminated are unique symbols observed in those "
            "states today; dmr is the unique union of actual post-dedupe, Top-K DMR inbox "
            "selections today, not current per-scan occupancy",
        }

    def save(self) -> None:
        payload = {
            "anchor_date": self.anchor_date,
            "states": {k: sorted(v) for k, v in self.states.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def update_daily_unique(
    data_dir: Path,
    anchor_date: str,
    rows: list[dict[str, Any]],
    *,
    dmr_messages: Iterable[dict[str, Any]] = (),
    dmr_inbox: Path | None = None,
) -> dict[str, Any]:
    """Fold state occupancy and real post-Top-K DMR selections into one day ledger."""
    led = DailyUniqueLedger(data_dir / "daily_unique.json", anchor_date)
    if dmr_inbox is not None:
        led.seed_dmr_from_inbox(dmr_inbox)
    led.observe(rows)
    led.observe_dmr(dmr_messages)
    led.save()
    return led.counts()
