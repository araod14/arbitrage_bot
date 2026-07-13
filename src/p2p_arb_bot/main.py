"""Composition root: wiring de adaptadores, CLI, loop y cierre limpio.

Este es el ÚNICO módulo que conoce las implementaciones concretas. El resto del
código depende de abstracciones (puertos). Aquí se instancian los adaptadores y
se inyectan en el caso de uso ``MonitorService``.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

from rich.console import Console

from .application.monitor import MonitorService
from .config import AppConfig, Defaults
from .domain.models import WatchTarget
from .infrastructure.binance_p2p import BinanceP2PSource
from .infrastructure.console_notifier import ConsoleNotifier
from .infrastructure.discovery import discover_pay_methods
from .infrastructure.screenshot_notifier import ScreenshotNotifier
from .infrastructure.sqlite_repo import SQLiteRepository
from .infrastructure.status_notifier import StatusNotifier

logger = logging.getLogger(__name__)
console = Console()


def setup_logging(log_path: str) -> None:
    """Logging a archivo + consola."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream = logging.StreamHandler()
    stream.setLevel(logging.WARNING)  # consola limpia; el detalle va al archivo
    stream.setFormatter(fmt)
    root.addHandler(stream)


# --- Prompts interactivos ----------------------------------------------------

async def _ask(prompt: str, default: str) -> str:
    """input() sin bloquear el event loop, con valor por defecto."""
    suffix = f" [{default}]" if default != "" else ""
    raw = await asyncio.to_thread(input, f"{prompt}{suffix}: ")
    return raw.strip() or default


async def _ask_decimal(prompt: str, default: Decimal) -> Decimal:
    while True:
        raw = await _ask(prompt, str(default))
        try:
            return Decimal(raw)
        except InvalidOperation:
            console.print("[red]Valor numérico inválido, intenta de nuevo.[/]")


async def _ask_int(prompt: str, default: int) -> int:
    while True:
        raw = await _ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            console.print("[red]Entero inválido, intenta de nuevo.[/]")


async def _ask_bool(prompt: str, default: bool) -> bool:
    raw = await _ask(f"{prompt} (s/n)", "s" if default else "n")
    return raw.lower() in ("s", "si", "sí", "y", "yes", "1", "true")


async def _select_pay_methods(
    source: BinanceP2PSource, defaults: Defaults
) -> tuple[str, ...]:
    console.print("[cyan]Descubriendo métodos de pago disponibles...[/]")
    methods = await discover_pay_methods(source, defaults.asset, defaults.fiat)
    if not methods:
        console.print("[yellow]No se descubrieron métodos; se vigilarán todos.[/]")
        return ()

    console.print("[bold]Métodos de pago disponibles:[/]")
    for i, (ident, name) in enumerate(methods, start=1):
        console.print(f"  [green]{i:>2}[/]. {name}")
    console.print(
        "Elige uno o varios por número, separados por coma "
        "(Enter = todos los métodos)."
    )
    raw = await _ask("Selección", "")
    if not raw:
        return ()

    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        idx = int(part)
        if 1 <= idx <= len(methods):
            chosen.append(methods[idx - 1][0])
    return tuple(dict.fromkeys(chosen))  # dedup preservando orden


async def build_config(source: BinanceP2PSource, defaults: Defaults) -> AppConfig:
    """Construye la config: modo no interactivo o prompts con defaults del .env."""
    if defaults.no_input:
        target = WatchTarget(
            asset=defaults.asset,
            fiat=defaults.fiat,
            pay_methods=defaults.pay_methods,
            max_usdt=defaults.max_usdt,
            threshold_pct=defaults.threshold_pct,
            fee_buffer_pct=defaults.fee_buffer_pct,
            merchant_check=defaults.merchant_check,
            rows=defaults.rows,
            outlier_max_dev_pct=defaults.outlier_max_dev_pct,
        )
    else:
        console.print(
            f"[bold]Configuración para {defaults.asset}/{defaults.fiat}[/] "
            "(Enter para aceptar el valor por defecto)\n"
        )
        pay_methods = (
            defaults.pay_methods
            if defaults.pay_methods
            else await _select_pay_methods(source, defaults)
        )
        max_usdt = await _ask_decimal("Monto máximo en USDT", defaults.max_usdt)
        threshold = await _ask_decimal("Umbral de ganancia neta %", defaults.threshold_pct)
        fee_buffer = await _ask_decimal("Buffer de fees %", defaults.fee_buffer_pct)
        merchant = await _ask_bool("Solo comerciantes verificados", defaults.merchant_check)
        interval = await _ask_int("Intervalo de polling (s)", defaults.poll_interval_s)
        defaults.poll_interval_s = interval

        target = WatchTarget(
            asset=defaults.asset,
            fiat=defaults.fiat,
            pay_methods=pay_methods,
            max_usdt=max_usdt,
            threshold_pct=threshold,
            fee_buffer_pct=fee_buffer,
            merchant_check=merchant,
            rows=defaults.rows,
            outlier_max_dev_pct=defaults.outlier_max_dev_pct,
        )

    config = AppConfig(
        targets=[target],
        poll_interval_s=defaults.poll_interval_s,
        db_path=defaults.db_path,
        log_path=defaults.log_path,
        status_path=defaults.status_path,
        screenshots_dir=defaults.screenshots_dir,
        screenshots_enabled=defaults.screenshots_enabled,
        impersonate=defaults.impersonate,
        proxy=defaults.proxy,
        beep=defaults.beep,
        rows=defaults.rows,
    )
    config.validate()
    return config


async def amain(defaults: Defaults) -> None:
    source = BinanceP2PSource(impersonate=defaults.impersonate, proxy=defaults.proxy)
    repo: SQLiteRepository | None = None
    try:
        config = await build_config(source, defaults)
        repo = SQLiteRepository(config.db_path)
        notifier = ConsoleNotifier(beep=config.beep, console=console)
        status = StatusNotifier(config.status_path)
        shot = ScreenshotNotifier(
            config.screenshots_dir, enabled=config.screenshots_enabled
        )

        service = MonitorService(
            source=source,
            repositories=[repo],
            notifiers=[notifier, status, shot],
            poll_interval_s=config.poll_interval_s,
        )

        methods_lbl = ", ".join(config.targets[0].pay_methods) or "todos"
        console.print(
            f"\n[bold green]Monitoreando[/] {config.targets[0].label} "
            f"| métodos: {methods_lbl} "
            f"| umbral: {config.targets[0].threshold_pct}% "
            f"| cada ~{config.poll_interval_s}s. [dim]Ctrl+C para salir.[/]\n"
        )
        await service.run_forever(config.targets)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    finally:
        await source.aclose()
        if repo is not None:
            repo.close()
        console.print("[dim]Conexiones cerradas.[/]")


def cli() -> None:
    defaults = Defaults.from_env()
    setup_logging(defaults.log_path)
    try:
        asyncio.run(amain(defaults))
    except KeyboardInterrupt:
        console.print("\n[bold]Saliendo limpiamente.[/]")


if __name__ == "__main__":
    cli()
