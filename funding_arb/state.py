"""State persistence + ledger for funding rate arb."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = STATE_DIR / "funding_state.json"
LEDGER_FILE = STATE_DIR / "funding_ledger.jsonl"
WATCHLIST_FILE = STATE_DIR / "funding_watchlist.json"


def save_state(state: dict) -> None:
    """Write current position state to disk."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    logger.debug("State saved: %s", state.get("state", "?"))


def load_state() -> dict | None:
    """Load position state from disk. Returns None if no state file."""
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("Failed to load state: %s", e)
        return None


def clear_state() -> None:
    """Remove state file (position closed cleanly)."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        logger.debug("State file cleared")


def append_ledger(entry: dict) -> None:
    """Append a closed position record to the ledger."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry["closed_at"] = datetime.now(timezone.utc).isoformat()
    with open(LEDGER_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info("Ledger entry: %s net=$%.4f", entry.get("symbol", "?"), entry.get("net_pnl", 0))


def read_ledger() -> list[dict]:
    """Read all ledger entries."""
    if not LEDGER_FILE.exists():
        return []
    entries = []
    with open(LEDGER_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def save_watchlist(symbols: list[str]) -> None:
    """Save watchlist of symbols to scan."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "symbols": symbols,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_watchlist() -> list[str]:
    """Load watchlist. Returns empty list if none."""
    if not WATCHLIST_FILE.exists():
        return []
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return data.get("symbols", [])
    except (json.JSONDecodeError, IOError):
        return []


def watchlist_age_hours() -> float:
    """How many hours since watchlist was last updated."""
    if not WATCHLIST_FILE.exists():
        return 999.0
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        updated = datetime.fromisoformat(data["updated"])
        delta = datetime.now(timezone.utc) - updated
        return delta.total_seconds() / 3600
    except Exception:
        return 999.0
