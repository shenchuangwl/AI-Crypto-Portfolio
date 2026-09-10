"""Load monorepo .env into os.environ (no external deps)."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """Parse KEY=VALUE lines. Existing env wins (does not override)."""
    candidates = []
    if path:
        candidates.append(Path(path))
    root = Path(os.environ.get("HERMES_ROOT", Path(__file__).resolve().parents[4]))
    candidates.extend(
        [
            root / ".env",
            Path.cwd() / ".env",
            Path.home() / ".hermes" / ".env",
        ]
    )
    for p in candidates:
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        return p
    return None
