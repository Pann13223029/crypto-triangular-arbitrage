"""Profit calculator — vectorized profit computation with fee & slippage accounting."""

import numpy as np

from core.models import (
    Direction,
    OrderBook,
    OrderSide,
    Opportunity,
    Ticker,
    Triangle,
    TriangleLeg,
)


class ProfitCalculator:
    """
    Calculates arbitrage profit for triangles.

    Supports both theoretical (ticker-based) and executable
    (order-book-based) profit calculation.
    """

    def __init__(self, fee_rate: float = 0.00075):
        """
        Args:
            fee_rate: Per-trade fee rate (e.g., 0.00075 for 0.075% with BNB).
        """
        self.fee_rate = fee_rate
        # Fee multiplier applied per leg: (1 - fee) for buy, (1 - fee) for sell
        self._fee_mult = 1.0 - fee_rate
        # Total fee drag for 3 legs
        self._total_fee_mult = self._fee_mult ** 3

    def leg_rate(self, leg: TriangleLeg, ticker: Ticker) -> float:
        """
        Get the exchange rate for a single leg.

        BUY leg:  We spend quote to get base → rate = 1 / ask
                  (how much base we get per unit of quote)
        SELL leg: We sell base to get quote → rate = bid
                  (how much quote we get per unit of base)
        """
        if leg.side == OrderSide.BUY:
            if ticker.ask <= 0:
                return 0.0
            return 1.0 / ticker.ask
        else:
            return ticker.bid

    def triangle_profit(
        self,
        triangle: Triangle,
        tickers: dict[str, Ticker],
    ) -> tuple[float, float, Direction]:
        """
        Calculate theoretical profit for a triangle in both directions.

        Returns:
            (forward_profit, reverse_profit, best_direction)
            Profits are net of fees. Positive = profitable.
        """
        # Forward: A → B → C → A
        fwd = self._path_profit(triangle.forward_legs, tickers)

        # Reverse: A → C → B → A
        rev = self._path_profit(triangle.reverse_legs, tickers)

        if fwd >= rev:
            return fwd, rev, Direction.FORWARD
        return fwd, rev, Direction.REVERSE

    def _path_profit(
        self,
        legs: tuple[TriangleLeg, ...],
        tickers: dict[str, Ticker],
    ) -> float:
        """
        Calculate net profit ratio for a path of legs.

        Returns the net profit as a fraction (e.g., 0.001 = 0.1%).
        Negative means unprofitable.
        """
        product = 1.0
        for leg in legs:
            ticker = tickers.get(leg.symbol)
            if ticker is None or ticker.bid <= 0 or ticker.ask <= 0:
                return -1.0  # Missing price data
            product *= self.leg_rate(leg, ticker)

        # Apply fees for all 3 legs
        net = product * self._total_fee_mult - 1.0
        return net

    def batch_calculate(
        self,
        triangles: list[Triangle],
        tickers: dict[str, Ticker],
        min_profit: float = 0.0,
    ) -> list[Opportunity]:
        """
        Vectorized profit calculation for multiple triangles.

        Uses numpy for batch computation of all forward and reverse
        profits simultaneously.

        Args:
            triangles: List of triangles to evaluate.
            tickers: Current price data.
            min_profit: Minimum net profit threshold to include.

        Returns:
            List of profitable Opportunity objects.
        """
        n = len(triangles)
        if n == 0:
            return []

        # Build rate arrays for forward and reverse paths
        fwd_rates = np.ones((n, 3), dtype=np.float64)
        rev_rates = np.ones((n, 3), dtype=np.float64)

        valid_mask = np.ones(n, dtype=bool)

        for i, tri in enumerate(triangles):
            for j, leg in enumerate(tri.forward_legs):
                ticker = tickers.get(leg.symbol)
                if ticker is None or ticker.bid <= 0 or ticker.ask <= 0:
                    valid_mask[i] = False
                    break
                fwd_rates[i, j] = self.leg_rate(leg, ticker)

            if not valid_mask[i]:
                continue

            for j, leg in enumerate(tri.reverse_legs):
                ticker = tickers.get(leg.symbol)
                if ticker is None or ticker.bid <= 0 or ticker.ask <= 0:
                    valid_mask[i] = False
                    break
                rev_rates[i, j] = self.leg_rate(leg, ticker)

        # Vectorized product across legs
        fwd_products = np.prod(fwd_rates, axis=1)
        rev_products = np.prod(rev_rates, axis=1)

        # Apply fees
        fwd_profits = fwd_products * self._total_fee_mult - 1.0
        rev_profits = rev_products * self._total_fee_mult - 1.0

        # Best direction per triangle
        best_profits = np.maximum(fwd_profits, rev_profits)
        is_forward = fwd_profits >= rev_profits

        # Filter: valid and above threshold
        profitable = valid_mask & (best_profits > min_profit)

        # Build opportunity objects
        opportunities: list[Opportunity] = []
        indices = np.where(profitable)[0]

        for i in indices:
            tri = triangles[i]
            direction = Direction.FORWARD if is_forward[i] else Direction.REVERSE
            profit = float(best_profits[i])

            # Capture current prices for this triangle
            prices = {}
            for leg in tri.forward_legs:
                if leg.symbol in tickers:
                    prices[leg.symbol] = tickers[leg.symbol]

            opp = Opportunity(
                triangle=tri,
                direction=direction,
                theoretical_profit=profit,
                executable_profit=None,  # Set by order book calc
                prices=prices,
            )
            opportunities.append(opp)

        # Sort by profit descending
        opportunities.sort(key=lambda o: o.theoretical_profit, reverse=True)
        return opportunities

    def executable_profit(
        self,
        opportunity: Opportunity,
        order_books: dict[str, OrderBook],
        position_size_quote: float,
    ) -> float | None:
        """
        Calculate executable profit using order book depth.

        Simulates walking the order book for each leg to get
        realistic fill prices accounting for slippage.

        Args:
            opportunity: The opportunity to evaluate.
            order_books: Current order book data.
            position_size_quote: Position size in the starting asset.

        Returns:
            Net profit ratio after slippage + fees, or None if
            insufficient liquidity.
        """
        tri = opportunity.triangle
        legs = (
            tri.forward_legs
            if opportunity.direction == Direction.FORWARD
            else tri.reverse_legs
        )

        # Track how much we have as we move through legs
        current_amount = position_size_quote

        for leg in legs:
            book = order_books.get(leg.symbol)
            if book is None:
                return None

            if leg.side == OrderSide.BUY:
                # Spending current_amount of quote to buy base
                # Walk up the asks
                avg_price = book.executable_buy_price(
                    current_amount / book.best_ask  # Approximate qty
                )
                if avg_price is None:
                    return None
                current_amount = (current_amount / avg_price) * self._fee_mult

            else:
                # Selling current_amount of base for quote
                avg_price = book.executable_sell_price(current_amount)
                if avg_price is None:
                    return None
                current_amount = current_amount * avg_price * self._fee_mult

        # Profit ratio
        profit = (current_amount / position_size_quote) - 1.0
        return profit
