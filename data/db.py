"""SQLite database for trade logging and opportunity tracking."""

import logging
import aiosqlite

from config.settings import DatabaseConfig
from core.models import Direction, Opportunity, Order, OrderStatus

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time INTEGER NOT NULL,
    end_time INTEGER,
    mode TEXT NOT NULL,
    total_opportunities INTEGER DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0.0,
    net_pnl REAL DEFAULT 0.0,
    fees_paid REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    timestamp_ms INTEGER NOT NULL,
    triangle_path TEXT NOT NULL,
    direction TEXT NOT NULL,
    theoretical_profit REAL NOT NULL,
    executable_profit REAL,
    executed INTEGER DEFAULT 0,
    skip_reason TEXT DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER,
    leg_number INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    expected_price REAL,
    actual_price REAL,
    quantity REAL,
    fee REAL DEFAULT 0.0,
    slippage REAL DEFAULT 0.0,
    status TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
);

CREATE INDEX IF NOT EXISTS idx_opp_session ON opportunities(session_id);
CREATE INDEX IF NOT EXISTS idx_opp_timestamp ON opportunities(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_trades_opp ON trades(opportunity_id);
"""


class Database:
    """Async SQLite database for logging trades and opportunities."""

    def __init__(self, config: DatabaseConfig | None = None):
        self.config = config or DatabaseConfig()
        self._db: aiosqlite.Connection | None = None
        self._session_id: int | None = None

    async def connect(self) -> None:
        """Open database connection and create tables."""
        self._db = await aiosqlite.connect(self.config.db_path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("Database connected: %s", self.config.db_path)

    async def start_session(self, mode: str) -> int:
        """Start a new trading session. Returns session ID."""
        from time import time_ns
        ts = time_ns() // 1_000_000

        cursor = await self._db.execute(
            "INSERT INTO sessions (start_time, mode) VALUES (?, ?)",
            (ts, mode),
        )
        await self._db.commit()
        self._session_id = cursor.lastrowid
        logger.info("Started session %d (%s mode)", self._session_id, mode)
        return self._session_id

    async def end_session(
        self, gross_pnl: float = 0.0, net_pnl: float = 0.0, fees_paid: float = 0.0,
    ) -> None:
        """Close the current session with final P&L."""
        if self._session_id is None:
            return

        from time import time_ns
        ts = time_ns() // 1_000_000

        # Count totals
        row = await (await self._db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE session_id = ?",
            (self._session_id,),
        )).fetchone()
        total_opps = row[0] if row else 0

        row = await (await self._db.execute(
            """SELECT COUNT(*) FROM trades t
               JOIN opportunities o ON t.opportunity_id = o.id
               WHERE o.session_id = ?""",
            (self._session_id,),
        )).fetchone()
        total_trades = row[0] if row else 0

        await self._db.execute(
            """UPDATE sessions SET end_time=?, total_opportunities=?,
               total_trades=?, gross_pnl=?, net_pnl=?, fees_paid=?
               WHERE id=?""",
            (ts, total_opps, total_trades, gross_pnl, net_pnl, fees_paid, self._session_id),
        )
        await self._db.commit()
        logger.info("Ended session %d (opps=%d, trades=%d, pnl=%.6f)",
                     self._session_id, total_opps, total_trades, net_pnl)

    async def log_opportunity(self, opp: Opportunity) -> int:
        """Log a detected opportunity. Returns opportunity ID."""
        path = " → ".join(opp.triangle.assets)

        cursor = await self._db.execute(
            """INSERT INTO opportunities
               (session_id, timestamp_ms, triangle_path, direction,
                theoretical_profit, executable_profit, executed, skip_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self._session_id,
                opp.timestamp_ms,
                path,
                opp.direction.value,
                opp.theoretical_profit,
                opp.executable_profit,
                1 if opp.executed else 0,
                opp.skip_reason,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def log_trade(self, opp_id: int, leg_number: int, order: Order) -> int:
        """Log a single trade leg."""
        cursor = await self._db.execute(
            """INSERT INTO trades
               (opportunity_id, leg_number, symbol, side,
                expected_price, actual_price, quantity, fee,
                slippage, status, timestamp_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opp_id,
                leg_number,
                order.symbol,
                order.side.value,
                order.expected_price,
                order.actual_price,
                order.quantity,
                order.fee,
                order.slippage,
                order.status.value,
                order.timestamp_ms,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_session_summary(self) -> dict | None:
        """Get summary of current session."""
        if self._session_id is None:
            return None

        row = await (await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (self._session_id,),
        )).fetchone()

        if row is None:
            return None

        return {
            "session_id": row[0],
            "start_time": row[1],
            "end_time": row[2],
            "mode": row[3],
            "total_opportunities": row[4],
            "total_trades": row[5],
            "gross_pnl": row[6],
            "net_pnl": row[7],
            "fees_paid": row[8],
        }

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
