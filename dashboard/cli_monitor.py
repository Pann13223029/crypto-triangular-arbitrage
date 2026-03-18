"""Real-time CLI dashboard using Rich library."""

import asyncio
import logging
from datetime import datetime, timedelta
from time import time_ns

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.scanner import TriangleScanner
from data.price_cache import PriceCache
from exchange.binance_ws import BinanceWebSocket
from exchange.simulator import SimulatedExchange
from execution.executor import Executor
from execution.order_manager import OrderManager
from execution.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class Dashboard:
    """
    Real-time CLI dashboard displaying system status.

    Panels:
    - System status (mode, uptime, WebSocket health)
    - Scanner stats (ticks, scans, opportunities, hit rate)
    - Risk manager (daily P&L, kill switch, cooldown)
    - Balances (virtual portfolio)
    - Recent trades (last 10)
    - Top opportunities (last 10 seen)
    """

    def __init__(
        self,
        scanner: TriangleScanner,
        price_cache: PriceCache,
        ws: BinanceWebSocket,
        exchange: SimulatedExchange,
        executor: Executor,
        risk_manager: RiskManager,
        order_manager: OrderManager,
        mode: str = "simulation",
    ):
        self.scanner = scanner
        self.price_cache = price_cache
        self.ws = ws
        self.exchange = exchange
        self.executor = executor
        self.risk_manager = risk_manager
        self.order_manager = order_manager
        self.mode = mode

        self.console = Console()
        self.start_time = time_ns() // 1_000_000
        self.recent_opportunities: list[dict] = []
        self._max_recent = 10

    def record_opportunity(self, path: str, profit: float, executed: bool, reason: str = "") -> None:
        """Record an opportunity for display."""
        self.recent_opportunities.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "path": path,
            "profit": profit,
            "executed": executed,
            "reason": reason,
        })
        if len(self.recent_opportunities) > self._max_recent:
            self.recent_opportunities.pop(0)

    def _build_layout(self) -> Layout:
        """Build the dashboard layout."""
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )

        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )

        layout["left"].split_column(
            Layout(name="status", size=10),
            Layout(name="scanner", size=10),
            Layout(name="risk", size=12),
        )

        layout["right"].split_column(
            Layout(name="balances", size=12),
            Layout(name="trades"),
        )

        return layout

    def _render_header(self) -> Panel:
        """Render header with title and mode."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uptime_ms = (time_ns() // 1_000_000) - self.start_time
        uptime = str(timedelta(milliseconds=uptime_ms)).split(".")[0]

        mode_color = "green" if self.mode == "simulation" else "red bold"
        title = Text()
        title.append("CRYPTO TRIANGULAR ARBITRAGE", style="bold cyan")
        title.append("  │  ", style="dim")
        title.append(f"Mode: {self.mode.upper()}", style=mode_color)
        title.append("  │  ", style="dim")
        title.append(f"Uptime: {uptime}", style="white")
        title.append("  │  ", style="dim")
        title.append(now, style="dim")

        return Panel(title, style="blue")

    def _render_status(self) -> Panel:
        """Render system status panel."""
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column("Key", style="dim", width=20)
        table.add_column("Value")

        # WebSocket
        ws_stats = self.ws.stats()
        ws_status = "[green]CONNECTED[/]" if ws_stats["connected"] else "[red]DISCONNECTED[/]"
        ws_health = "[green]HEALTHY[/]" if ws_stats["healthy"] else "[red]STALE[/]"

        table.add_row("WebSocket", ws_status)
        table.add_row("Health", ws_health)
        table.add_row("Messages", f"{ws_stats['total_messages']:,}")
        table.add_row("Reconnects", str(ws_stats["total_reconnects"]))
        table.add_row("Price Cache", f"{'[green]OK' if not self.price_cache.is_stale() else '[red]STALE'}[/]")
        table.add_row("Tracked Symbols", str(self.price_cache.stats()["tracked_tickers"]))

        return Panel(table, title="[bold]System Status[/]", border_style="blue")

    def _render_scanner(self) -> Panel:
        """Render scanner statistics panel."""
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column("Key", style="dim", width=20)
        table.add_column("Value")

        stats = self.scanner.stats()

        table.add_row("Total Ticks", f"{stats['total_ticks']:,}")
        table.add_row("Triangle Scans", f"{stats['total_triangle_scans']:,}")
        table.add_row("Opportunities", f"[yellow]{stats['total_opportunities']}[/]")
        table.add_row("Hit Rate", stats["hit_rate"])
        table.add_row("Triangles Loaded", str(len(self.scanner.graph.triangles)))

        # Ticks per second
        elapsed_s = max((time_ns() // 1_000_000 - self.start_time) / 1000, 1)
        tps = stats["total_ticks"] / elapsed_s
        table.add_row("Ticks/sec", f"{tps:,.0f}")

        return Panel(table, title="[bold]Scanner[/]", border_style="cyan")

    def _render_risk(self) -> Panel:
        """Render risk manager panel."""
        table = Table(show_header=False, expand=True, box=None, padding=(0, 1))
        table.add_column("Key", style="dim", width=20)
        table.add_column("Value")

        stats = self.risk_manager.stats()
        exec_stats = self.executor.stats()

        # P&L with color
        pnl = exec_stats["net_pnl"]
        pnl_color = "green" if pnl >= 0 else "red"
        table.add_row("Net P&L", f"[{pnl_color}]${pnl:,.6f}[/]")
        table.add_row("Gross Profit", f"[green]${exec_stats['total_profit']:,.6f}[/]")
        table.add_row("Gross Loss", f"[red]${exec_stats['total_loss']:,.6f}[/]")
        table.add_row("Executions", str(exec_stats["total_executions"]))
        table.add_row("Aborts", str(exec_stats["total_aborts"]))

        # Kill switch
        if stats["killed"]:
            table.add_row("Kill Switch", f"[red bold]ACTIVE: {stats['kill_reason']}[/]")
        else:
            table.add_row("Kill Switch", "[green]OFF[/]")

        table.add_row("Daily P&L", f"${stats['daily_pnl']:,.4f}")
        table.add_row("Consec. Losses", str(stats["consecutive_losses"]))
        table.add_row("Approved/Rejected", f"{stats['total_approved']}/{stats['total_rejected']}")

        border = "red" if stats["killed"] else "yellow"
        return Panel(table, title="[bold]Risk & P&L[/]", border_style=border)

    def _render_balances(self) -> Panel:
        """Render virtual balances panel."""
        table = Table(expand=True, box=None, padding=(0, 1))
        table.add_column("Asset", style="bold", width=8)
        table.add_column("Balance", justify="right")
        table.add_column("~USD", justify="right", style="dim")

        balances = {k: v for k, v in self.exchange.balances.items() if abs(v) > 1e-8}

        for asset, amount in sorted(balances.items(), key=lambda x: -x[1]):
            # Estimate USD value
            usd_val = ""
            if asset == "USDT":
                usd_val = f"${amount:,.2f}"
            else:
                ticker = self.price_cache.get_ticker(f"{asset}USDT")
                if ticker and ticker.mid > 0:
                    usd_val = f"${amount * ticker.mid:,.2f}"

            table.add_row(asset, f"{amount:,.8f}", usd_val)

        # Total portfolio value
        total_usd = 0.0
        for asset, amount in balances.items():
            if asset == "USDT":
                total_usd += amount
            else:
                ticker = self.price_cache.get_ticker(f"{asset}USDT")
                if ticker and ticker.mid > 0:
                    total_usd += amount * ticker.mid

        table.add_row("─" * 6, "─" * 14, "─" * 10)
        table.add_row("[bold]TOTAL[/]", "", f"[bold]${total_usd:,.2f}[/]")

        return Panel(table, title="[bold]Balances[/]", border_style="green")

    def _render_trades(self) -> Panel:
        """Render recent trades and opportunities."""
        table = Table(expand=True, box=None, padding=(0, 0))
        table.add_column("Time", style="dim", width=8)
        table.add_column("Triangle", width=24)
        table.add_column("Profit", justify="right", width=10)
        table.add_column("Status", width=14)

        om_stats = self.order_manager.stats()

        for entry in reversed(self.recent_opportunities):
            profit_str = f"{entry['profit']:.4%}"
            if entry["executed"]:
                status = "[green]EXECUTED[/]"
                profit_style = "green" if entry["profit"] > 0 else "red"
            else:
                status = f"[dim]{entry['reason'][:14]}[/]"
                profit_style = "yellow"

            table.add_row(
                entry["time"],
                entry["path"],
                f"[{profit_style}]{profit_str}[/]",
                status,
            )

        if not self.recent_opportunities:
            table.add_row("", "[dim]Waiting for opportunities...[/]", "", "")

        # Footer with order manager stats
        footer = Text()
        footer.append(f"Win Rate: {om_stats['win_rate']}", style="bold")
        footer.append(f"  │  Trades: {om_stats['total_trades']}", style="dim")
        footer.append(f"  │  Fees: ${om_stats['total_fees']:.6f}", style="dim")

        return Panel(
            table,
            title="[bold]Recent Opportunities[/]",
            subtitle=footer,
            border_style="magenta",
        )

    def _render_footer(self) -> Panel:
        """Render footer with controls."""
        text = Text()
        text.append(" Ctrl+C", style="bold yellow")
        text.append(" Stop  ", style="dim")
        text.append("│", style="dim")
        text.append("  Scanning ", style="dim")
        text.append(f"{len(self.scanner.graph.triangles):,}", style="cyan")
        text.append(" triangles across ", style="dim")
        text.append(f"{len(self.scanner.graph.get_subscribed_symbols()):,}", style="cyan")
        text.append(" symbols", style="dim")

        return Panel(text, style="dim")

    def render(self) -> Layout:
        """Render the complete dashboard."""
        layout = self._build_layout()

        layout["header"].update(self._render_header())
        layout["status"].update(self._render_status())
        layout["scanner"].update(self._render_scanner())
        layout["risk"].update(self._render_risk())
        layout["balances"].update(self._render_balances())
        layout["trades"].update(self._render_trades())
        layout["footer"].update(self._render_footer())

        return layout

    async def run(self, refresh_rate: float = 0.5) -> None:
        """
        Run the dashboard with live updates.

        Args:
            refresh_rate: Seconds between refreshes.
        """
        with Live(
            self.render(),
            console=self.console,
            refresh_per_second=int(1 / refresh_rate),
            screen=True,
        ) as live:
            try:
                while True:
                    live.update(self.render())
                    await asyncio.sleep(refresh_rate)
            except asyncio.CancelledError:
                pass
