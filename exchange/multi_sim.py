"""Multi-exchange simulator — generates divergent prices across simulated exchanges."""

import logging
import math
import random
from collections import defaultdict

from config.settings import FeeConfig, FeeSchedule, MultiSimConfig, SimulationConfig
from core.models import Ticker, TradingPair
from exchange.simulator import SimulatedExchange

logger = logging.getLogger(__name__)


class MultiExchangeSimulator:
    """
    Simulates multiple exchanges with divergent prices.

    Uses Ornstein-Uhlenbeck process to generate realistic
    mean-reverting price differences between exchanges.

    The base exchange receives the real ticker price. Each other
    exchange gets a slightly offset price that mean-reverts over time.
    """

    def __init__(self, config: MultiSimConfig | None = None):
        self.config = config or MultiSimConfig()

        self.exchanges: dict[str, SimulatedExchange] = {}
        self._ou_state: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )

        # Create simulated exchanges
        for ex_id in self.config.exchange_ids:
            fee_config = FeeConfig()
            sim_config = SimulationConfig(
                initial_balances=dict(self.config.initial_balances_per_exchange),
                latency_ms=0,
                fixed_slippage=0.0001,
            )
            self.exchanges[ex_id] = SimulatedExchange(
                fee_config=fee_config,
                sim_config=sim_config,
                exchange_id=ex_id,
            )

        self.base_id = self.config.exchange_ids[0]

        logger.info(
            "MultiExchangeSimulator: %d exchanges (%s)",
            len(self.exchanges),
            ", ".join(self.config.exchange_ids),
        )

    def load_pairs(self, pairs: list[TradingPair]) -> None:
        """Load trading pairs into all exchanges."""
        for ex in self.exchanges.values():
            ex.load_pairs(pairs)

    def get_exchange(self, exchange_id: str) -> SimulatedExchange:
        return self.exchanges[exchange_id]

    def get_fee_schedules(self) -> dict[str, FeeSchedule]:
        """Get fee schedules for all exchanges."""
        return {
            ex_id: ex.fee_schedule for ex_id, ex in self.exchanges.items()
        }

    def inject_base_ticker(self, ticker: Ticker) -> dict[str, Ticker]:
        """
        Inject a real price and generate divergent prices for all exchanges.

        The base exchange gets the exact price. Other exchanges get
        a price offset by an O-U mean-reverting process.

        Returns:
            dict of exchange_id -> Ticker (with divergent prices).
        """
        result: dict[str, Ticker] = {}

        for ex_id, exchange in self.exchanges.items():
            if ex_id == self.base_id:
                # Base exchange gets the real price
                exchange.inject_ticker(ticker)
                result[ex_id] = ticker
            else:
                # Other exchanges get offset price
                offset = self._ou_step(ex_id, ticker.symbol)
                offset_bid = ticker.bid * (1 + offset)
                offset_ask = ticker.ask * (1 + offset)

                divergent = Ticker(
                    symbol=ticker.symbol,
                    bid=offset_bid,
                    ask=offset_ask,
                    timestamp_ms=ticker.timestamp_ms,
                )
                exchange.inject_ticker(divergent)
                result[ex_id] = divergent

        return result

    def _ou_step(self, exchange_id: str, symbol: str) -> float:
        """
        Single Ornstein-Uhlenbeck step.

        dx = theta * (mu - x) * dt + sigma * sqrt(dt) * N(0,1)

        Returns a price offset multiplier (e.g., 0.001 = +0.1%).
        """
        x = self._ou_state[exchange_id][symbol]
        theta = self.config.ou_theta
        mu = self.config.ou_mu
        sigma = self.config.ou_sigma
        dt = 0.1  # ~100ms between ticks

        dx = theta * (mu - x) * dt + sigma * math.sqrt(dt) * random.gauss(0, 1)
        x_new = x + dx

        self._ou_state[exchange_id][symbol] = x_new
        return x_new

    def stats(self) -> dict:
        return {
            "exchanges": len(self.exchanges),
            "exchange_ids": list(self.exchanges.keys()),
            "ou_params": {
                "theta": self.config.ou_theta,
                "sigma": self.config.ou_sigma,
                "mu": self.config.ou_mu,
            },
            "per_exchange": {
                ex_id: ex.stats() for ex_id, ex in self.exchanges.items()
            },
        }
