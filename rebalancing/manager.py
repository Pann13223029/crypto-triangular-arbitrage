"""RebalanceManager — threshold-based + opportunity-aware rebalancing."""

import logging
from time import time_ns

from config.settings import RebalanceConfig
from cross_exchange.balance_tracker import BalanceTracker
from cross_exchange.models import (
    CrossExchangeOpportunity,
    RebalanceDecision,
    Transfer,
    TransferStatus,
)

logger = logging.getLogger(__name__)


class RebalanceManager:
    """
    Manages capital rebalancing across exchanges.

    Two modes:
    1. Threshold-based: triggers when any exchange deviates >25% from target
    2. Opportunity-aware: biases arb execution toward trades that
       naturally rebalance (scoring bonus)

    Uses USDT as the rebalancing asset (stablecoin, fast/cheap chains).
    """

    def __init__(
        self,
        balance_tracker: BalanceTracker,
        config: RebalanceConfig | None = None,
    ):
        self.balance_tracker = balance_tracker
        self.config = config or RebalanceConfig()

        # Target allocation per exchange (defaults to equal split)
        self.targets: dict[str, float] = {}

        # Cooldown tracking: exchange_id -> last rebalance timestamp (ms)
        self._last_rebalance_ms: dict[str, int] = {}

        # History
        self.transfer_history: list[Transfer] = []
        self.total_transfers: int = 0
        self.total_transfer_fees: float = 0.0
        self.total_transferred_usd: float = 0.0

    def set_targets(self, exchange_ids: list[str]) -> None:
        """Set equal allocation targets for given exchanges."""
        if self.config.target_allocation:
            self.targets = dict(self.config.target_allocation)
        else:
            n = len(exchange_ids)
            self.targets = {ex_id: 1.0 / n for ex_id in exchange_ids}
        logger.info("Rebalance targets: %s", self.targets)

    def check_rebalance_needed(self) -> RebalanceDecision | None:
        """
        Check if any exchange needs rebalancing.

        Returns a RebalanceDecision with transfers if rebalancing is needed,
        or None if all balances are within threshold.
        """
        if not self.config.enabled or not self.targets:
            return None

        all_balances = self.balance_tracker.get_all()
        now_ms = time_ns() // 1_000_000

        # Calculate total USDT across all exchanges
        total_usdt = sum(
            bals.get("USDT", 0.0) for bals in all_balances.values()
        )

        if total_usdt <= 0:
            return None

        # Find deviations
        deviations: dict[str, float] = {}  # exchange_id -> deviation (positive = excess)
        for ex_id, target_frac in self.targets.items():
            current = all_balances.get(ex_id, {}).get("USDT", 0.0)
            target = total_usdt * target_frac
            if target > 0:
                deviation = (current - target) / target
                deviations[ex_id] = deviation

        # Check if any exceeds threshold
        needs_rebalance = any(
            abs(d) > self.config.deviation_threshold
            for d in deviations.values()
        )

        if not needs_rebalance:
            return None

        # Build transfer plan: move from excess to deficit
        excess = sorted(
            [(ex, d) for ex, d in deviations.items() if d > self.config.deviation_threshold],
            key=lambda x: -x[1],  # Most excess first
        )
        deficit = sorted(
            [(ex, d) for ex, d in deviations.items() if d < -self.config.deviation_threshold],
            key=lambda x: x[1],  # Most deficit first
        )

        if not excess or not deficit:
            return None

        transfers: list[Transfer] = []

        for ex_excess, dev_excess in excess:
            # Check cooldown
            last = self._last_rebalance_ms.get(ex_excess, 0)
            if (now_ms - last) < (self.config.cooldown_sec * 1000):
                continue

            current = all_balances.get(ex_excess, {}).get("USDT", 0.0)
            target = total_usdt * self.targets[ex_excess]
            available = current - target  # How much to send

            if available < self.config.min_rebalance_usd:
                continue

            for ex_deficit, dev_deficit in deficit:
                # Check cooldown for receiver
                last_recv = self._last_rebalance_ms.get(ex_deficit, 0)
                if (now_ms - last_recv) < (self.config.cooldown_sec * 1000):
                    continue

                deficit_current = all_balances.get(ex_deficit, {}).get("USDT", 0.0)
                deficit_target = total_usdt * self.targets[ex_deficit]
                needed = deficit_target - deficit_current

                if needed < self.config.min_rebalance_usd:
                    continue

                amount = min(available, needed)
                if amount < self.config.min_rebalance_usd:
                    continue

                transfers.append(Transfer(
                    from_exchange=ex_excess,
                    to_exchange=ex_deficit,
                    asset="USDT",
                    amount=round(amount, 2),
                    fee=self.config.transfer_fee_usd,
                    chain=self.config.preferred_chain,
                ))

                available -= amount
                if available < self.config.min_rebalance_usd:
                    break

        if not transfers:
            return None

        total_amount = sum(t.amount for t in transfers)
        total_fees = sum(t.fee for t in transfers)

        logger.info(
            "Rebalance needed: %d transfers, $%.2f total, $%.2f fees",
            len(transfers), total_amount, total_fees,
        )

        return RebalanceDecision(
            transfers=transfers,
            reason=f"Deviation threshold {self.config.deviation_threshold:.0%} exceeded",
            total_amount=total_amount,
            total_fees=total_fees,
        )

    async def execute_rebalance(self, decision: RebalanceDecision) -> list[Transfer]:
        """
        Execute rebalancing transfers (simulated).

        In simulation mode, directly moves USDT between exchange balances.
        In live mode, would use actual withdrawal/deposit APIs.
        """
        completed: list[Transfer] = []
        now_ms = time_ns() // 1_000_000

        for transfer in decision.transfers:
            # Get exchange references
            from_ex = self.balance_tracker.exchanges.get(transfer.from_exchange)
            to_ex = self.balance_tracker.exchanges.get(transfer.to_exchange)

            if from_ex is None or to_ex is None:
                transfer.status = TransferStatus.FAILED
                logger.warning(
                    "Transfer failed: exchange not found (%s → %s)",
                    transfer.from_exchange, transfer.to_exchange,
                )
                continue

            # Check sender balance
            sender_balance = await from_ex.get_balance(transfer.asset)
            if sender_balance < transfer.amount + transfer.fee:
                transfer.status = TransferStatus.FAILED
                logger.warning(
                    "Transfer failed: insufficient %s on %s (need %.2f, have %.2f)",
                    transfer.asset, transfer.from_exchange,
                    transfer.amount + transfer.fee, sender_balance,
                )
                continue

            # Simulate transfer: deduct from sender, credit to receiver
            # In simulation, we directly modify balances
            if hasattr(from_ex, 'balances') and hasattr(to_ex, 'balances'):
                from_ex.balances[transfer.asset] = (
                    from_ex.balances.get(transfer.asset, 0.0)
                    - transfer.amount - transfer.fee
                )
                to_ex.balances[transfer.asset] = (
                    to_ex.balances.get(transfer.asset, 0.0)
                    + transfer.amount
                )

            transfer.status = TransferStatus.CONFIRMED
            transfer.confirmed_ms = now_ms
            completed.append(transfer)

            # Update cooldown
            self._last_rebalance_ms[transfer.from_exchange] = now_ms
            self._last_rebalance_ms[transfer.to_exchange] = now_ms

            self.total_transfers += 1
            self.total_transfer_fees += transfer.fee
            self.total_transferred_usd += transfer.amount

            logger.info(
                "REBALANCE: %s → %s | $%.2f %s (fee: $%.2f) ✓",
                transfer.from_exchange, transfer.to_exchange,
                transfer.amount, transfer.asset, transfer.fee,
            )

        self.transfer_history.extend(completed)

        # Refresh balance tracker
        await self.balance_tracker.refresh_all()

        return completed

    def opportunity_rebalance_bonus(
        self, opp: CrossExchangeOpportunity
    ) -> float:
        """
        Score bonus for an opportunity that naturally rebalances.

        Buying on exchange A spends USDT there → good if A has excess.
        Selling on exchange B receives USDT there → good if B has deficit.

        Returns a bonus to add to the opportunity's spread score.
        """
        if not self.targets:
            return 0.0

        all_balances = self.balance_tracker.get_all()
        total_usdt = sum(
            bals.get("USDT", 0.0) for bals in all_balances.values()
        )
        if total_usdt <= 0:
            return 0.0

        bonus = 0.0

        # Buying spends USDT on buy_exchange
        buy_current = all_balances.get(opp.buy_exchange, {}).get("USDT", 0.0)
        buy_target = total_usdt * self.targets.get(opp.buy_exchange, 0.33)
        if buy_current > buy_target:
            bonus += 0.0001  # Small bonus: spending from excess

        # Selling receives USDT on sell_exchange
        sell_current = all_balances.get(opp.sell_exchange, {}).get("USDT", 0.0)
        sell_target = total_usdt * self.targets.get(opp.sell_exchange, 0.33)
        if sell_current < sell_target:
            bonus += 0.0001  # Small bonus: receiving into deficit

        return bonus

    def get_deviation_report(self) -> dict:
        """Current deviation status for all exchanges."""
        all_balances = self.balance_tracker.get_all()
        total_usdt = sum(
            bals.get("USDT", 0.0) for bals in all_balances.values()
        )

        report = {}
        for ex_id, target_frac in self.targets.items():
            current = all_balances.get(ex_id, {}).get("USDT", 0.0)
            target = total_usdt * target_frac
            deviation = (current - target) / target if target > 0 else 0

            report[ex_id] = {
                "current_usdt": round(current, 2),
                "target_usdt": round(target, 2),
                "deviation": f"{deviation:+.1%}",
                "needs_rebalance": abs(deviation) > self.config.deviation_threshold,
            }

        return report

    def stats(self) -> dict:
        return {
            "total_transfers": self.total_transfers,
            "total_transferred_usd": round(self.total_transferred_usd, 2),
            "total_transfer_fees": round(self.total_transfer_fees, 2),
            "targets": self.targets,
        }
