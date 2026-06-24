"""Adaptador ``Notifier`` para consola, con salida enriquecida (rich)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..domain.models import Opportunity, WatchTarget


class ConsoleNotifier:
    """Imprime oportunidades como panel destacado y heartbeats discretos."""

    def __init__(self, *, beep: bool = True, console: Console | None = None) -> None:
        self._beep = beep
        self._console = console or Console()

    @staticmethod
    def _method_label(method: str) -> str:
        # "ALL" es el sentinel interno para "sin filtrar por método".
        return "cualquier método" if method == "ALL" else method

    async def notify_opportunity(self, opp: Opportunity) -> None:
        if self._beep:
            print("\a", end="", flush=True)  # campana del terminal

        buy_method = self._method_label(opp.buy_pay_method)
        sell_method = self._method_label(opp.sell_pay_method)

        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column(justify="right", style="bold cyan")
        table.add_column()

        table.add_row("Ganancia NETA", f"[bold green]{opp.net_pct:.3f}%[/]")
        table.add_row("Spread bruto", f"{opp.spread_pct:.3f}%")
        table.add_row("Par", f"{opp.asset}/{opp.fiat}")
        table.add_row("Monto", f"{opp.max_usdt} {opp.asset}")
        table.add_row(
            "Comprar a",
            f"{opp.buy_price} {opp.fiat}  vía [b]{buy_method}[/]  "
            f"(@{opp.buy_advertiser})",
        )
        table.add_row(
            "Vender a",
            f"{opp.sell_price} {opp.fiat}  vía [b]{sell_method}[/]  "
            f"(@{opp.sell_advertiser})",
        )
        table.add_row(
            "Profit est.",
            f"~{opp.est_profit_fiat:.2f} {opp.fiat}  "
            f"([bold]~{opp.est_profit_usdt:.2f} {opp.asset}[/])",
        )
        table.add_row("Link compra", f"[link={opp.buy_url}]{opp.buy_url}[/link]")
        table.add_row("Link venta", f"[link={opp.sell_url}]{opp.sell_url}[/link]")

        ts = opp.detected_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self._console.print(
            Panel(
                table,
                title="[bold white on green] OPORTUNIDAD DE ARBITRAJE [/]",
                subtitle=f"detectada {ts}",
                border_style="green",
                expand=False,
            )
        )

    async def notify_heartbeat(
        self, target: WatchTarget, best_spread: Decimal | None
    ) -> None:
        ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        if best_spread is None:
            body = Text("sin datos / sin pares elegibles", style="dim")
        else:
            color = "yellow" if best_spread >= 0 else "red"
            body = Text(f"mejor spread neto {best_spread:.3f}%", style=color)
        line = Text(f"[{ts}] {target.label}  ", style="dim")
        line.append(body)
        self._console.print(line)
