from __future__ import annotations

import argparse

from .service import MarketIngestService


def main() -> None:
    p = argparse.ArgumentParser(description="Hermes market-ingest (Binance native)")
    p.add_argument(
        "--once",
        action="store_true",
        help="Refresh universe + REST bootstrap then exit (no WS loop)",
    )
    p.add_argument(
        "--no-ws",
        action="store_true",
        help="Disable websocket (REST-only loop)",
    )
    args = p.parse_args()
    svc = MarketIngestService()
    if args.no_ws:
        svc.settings.enable_ws = False
    svc.run(once=args.once)


if __name__ == "__main__":
    main()
