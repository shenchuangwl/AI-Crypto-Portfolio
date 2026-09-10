# dmr-adapter (S4)

Bridges **coin-selection candidates** → **third_party DMR** consumption path.

## Layout

```
data/dmr-adapter/
  inbox/       # written by coin-selection (or --seed-example)
  accepted/    # passed §39.3 filters
  rejected/    # with reason
  outbox/      # per-scan result summary
```

DMR tree: `third_party/DMR_binance_Version_V16_A1/` (not modified).

## Run

```bash
export HERMES_ROOT=...
PYTHONPATH=services/dmr-adapter/src python3 -m dmr_adapter --probe-dmr
PYTHONPATH=services/dmr-adapter/src python3 -m dmr_adapter --seed-example --process-inbox
```

## Reject rules (§39.3)

`local_reject()` (and the pydantic `dmr_should_reject`) drop a message when:
non-`CONFIRMED` state, `NEUTRAL` direction, past `expires_at_utc`,
`data_mode == MISSING` or `data_confidence < 60`, any `H*` risk flag,
missing/≤0 `circulating_supply`, or an **unknown `parameter_version`**.

```bash
# default whitelist; override with a comma-separated list
DMR_PARAM_WHITELIST=param-v1.3.0-dual-path-sticky,param-v1.2.0-g1g2g3g4-path
```

The whitelist is the guard against silent parameter drift: if coin-selection ships a
new `parameter_version` without this list being updated, the whole batch is rejected
with reason `parameter_version` instead of being executed under unreviewed thresholds.

Messages arrive already deduplicated (one direction per symbol) and Top-K trimmed by
coin-selection; `confirm_path` (`S`/`M`) rides along for position sizing. `QUALIFIED`
and `READY_CONFIRM` rows never reach this inbox.

Executor integration (next): poll `accepted/` or implement PG NOTIFY; call existing
`main.py` / bit strategy with selected symbols — **out of S4 scope**.
