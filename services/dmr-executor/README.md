# dmr-executor (paper)

Consumes `data/dmr-adapter/accepted/*.json` and records **paper** decisions.

Does **not** call Binance order APIs. Probes `third_party/DMR_binance_Version_V16_A1`
for layout only.

```bash
export HERMES_ROOT=...
PYTHONPATH=services/dmr-executor/src python3 -m dmr_executor --probe
PYTHONPATH=services/dmr-executor/src python3 -m dmr_executor --paper
```
