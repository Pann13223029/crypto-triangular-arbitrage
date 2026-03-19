"""Tests for funding rate arb timing and state utilities."""

import json
import os
import pytest
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from funding_arb.timing import (
    next_funding_timestamp,
    prev_funding_timestamp,
    minutes_until_next_funding,
    just_passed_funding,
    in_entry_window,
    funding_info,
    FUNDING_HOURS,
)
from funding_arb import state


class TestTiming:

    def test_next_funding_is_future(self):
        nxt = next_funding_timestamp()
        assert nxt > datetime.now(timezone.utc)

    def test_prev_funding_is_past(self):
        prev = prev_funding_timestamp()
        assert prev <= datetime.now(timezone.utc)

    def test_next_funding_is_valid_hour(self):
        nxt = next_funding_timestamp()
        assert nxt.hour in FUNDING_HOURS
        assert nxt.minute == 0
        assert nxt.second == 0

    def test_prev_funding_is_valid_hour(self):
        prev = prev_funding_timestamp()
        assert prev.hour in FUNDING_HOURS

    def test_minutes_until_positive(self):
        mins = minutes_until_next_funding()
        assert mins > 0
        assert mins <= 480  # Max 8 hours

    def test_funding_info_has_keys(self):
        info = funding_info()
        assert "next_funding" in info
        assert "minutes_until" in info
        assert "in_entry_window" in info


class TestState:

    def setup_method(self):
        """Use temp directory for state files."""
        self._orig_dir = state.STATE_DIR
        self._orig_file = state.STATE_FILE
        self._orig_ledger = state.LEDGER_FILE
        self._tmp = Path(tempfile.mkdtemp())
        state.STATE_DIR = self._tmp
        state.STATE_FILE = self._tmp / "test_state.json"
        state.LEDGER_FILE = self._tmp / "test_ledger.jsonl"

    def teardown_method(self):
        state.STATE_DIR = self._orig_dir
        state.STATE_FILE = self._orig_file
        state.LEDGER_FILE = self._orig_ledger

    def test_save_and_load_state(self):
        state.save_state({"state": "MONITORING", "symbol": "ETHUSDTM"})
        loaded = state.load_state()
        assert loaded is not None
        assert loaded["symbol"] == "ETHUSDTM"
        assert "last_updated" in loaded

    def test_load_empty(self):
        assert state.load_state() is None

    def test_clear_state(self):
        state.save_state({"state": "test"})
        state.clear_state()
        assert state.load_state() is None

    def test_append_and_read_ledger(self):
        state.append_ledger({"symbol": "ETHUSDTM", "net_pnl": 0.05})
        state.append_ledger({"symbol": "SOLUSDTM", "net_pnl": -0.02})

        entries = state.read_ledger()
        assert len(entries) == 2
        assert entries[0]["symbol"] == "ETHUSDTM"
        assert entries[1]["net_pnl"] == -0.02

    def test_read_empty_ledger(self):
        assert state.read_ledger() == []

    def test_watchlist(self):
        symbols = ["ETHUSDTM", "SOLUSDTM", "BTCUSDTM"]
        state.WATCHLIST_FILE = self._tmp / "test_watchlist.json"
        state.save_watchlist(symbols)
        loaded = state.load_watchlist()
        assert loaded == symbols

    def test_watchlist_age(self):
        state.WATCHLIST_FILE = self._tmp / "test_watchlist.json"
        state.save_watchlist(["X"])
        age = state.watchlist_age_hours()
        assert age < 0.01  # Just saved

    def test_watchlist_age_missing(self):
        state.WATCHLIST_FILE = self._tmp / "nonexistent.json"
        assert state.watchlist_age_hours() > 100
