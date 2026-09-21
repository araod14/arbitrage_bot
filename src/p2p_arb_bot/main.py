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
from .config import SUPPORTED_ASSETS, AppConfig, Defaults
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


async def _select_assets(defaults: Defaults) -> tuple[str, ...]:
    """Menú numerado de criptos a vigilar. Enter = mantener las del ``.env``."""
    console.print("[bold]Monedas disponibles en P2P:[/]")
    for i, name in enumerate(SUPPORTED_ASSETS, start=1):
        marca = "[green]*[/]" if name in defaults.assets else " "
        console.print(f"  {marca} [green]{i:>2}[/]. {name}")
    console.print(
        "Elige una o varias por número, separadas por coma "
        f"(Enter = {', '.join(defaults.assets)})."
    )
    raw = await _ask("Selección", "")
    if not raw:
        return defaults.assets

    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        idx = int(part)
        if 1 <= idx <= len(SUPPORTED_ASSETS):
            chosen.append(SUPPORTED_ASSETS[idx - 1])
    # Sin selección válida se mantienen las del .env: mejor eso que quedarse sin
    # ningún target y abortar por validación.
    return tuple(dict.fromkeys(chosen)) or defaults.assets


async def _select_pay_methods(
    source: BinanceP2PSource, defaults: Defaults
) -> tuple[str, ...]:
    console.print("[cyan]Descubriendo métodos de pago disponibles...[/]")
    # Cada libro puede tener métodos distintos; conserva la unión sin duplicados.
    available: dict[str, str] = {}
    for asset in dict.fromkeys(defaults.assets):
        try:
            available.update(await discover_pay_methods(source, asset, defaults.fiat))
        except Exception:
            logger.warning("No se pudieron descubrir métodos para %s/%s", asset, defaults.fiat)
    methods = sorted(available.items(), key=lambda item: item[0].lower())
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
        assets = defaults.assets
        pay_methods = defaults.pay_methods
        max_fiat = defaults.max_fiat
        threshold = defaults.threshold_pct
        fee_buffer = defaults.fee_buffer_pct
        merchant = defaults.merchant_check
    else:
        console.print(
            f"[bold]Configuración contra {defaults.fiat}[/] "
            "(Enter para aceptar el valor por defecto)\n"
        )
        assets = await _select_assets(defaults)
        defaults.assets = assets  # descubre métodos de todas las seleccionadas
        pay_methods = (
            defaults.pay_methods
            if defaults.pay_methods
            else await _select_pay_methods(source, defaults)
        )
        max_fiat = await _ask_decimal(
            f"Fondo disponible ({defaults.fiat})", defaults.max_fiat
        )
        threshold = await _ask_decimal("Umbral de ganancia neta %", defaults.threshold_pct)
        fee_buffer = await _ask_decimal("Buffer de fees %", defaults.fee_buffer_pct)
        merchant = await _ask_bool("Solo comerciantes verificados", defaults.merchant_check)
        defaults.poll_interval_s = await _ask_int(
            "Intervalo de polling (s)", defaults.poll_interval_s
        )

    # Un target por moneda: mismos parámetros, distinto asset. El motor los recorre
    # en cada barrido sin cambios estructurales.
    targets = [
        WatchTarget(
            asset=asset,
            fiat=defaults.fiat,
            pay_methods=pay_methods,
            max_fiat=max_fiat,
            threshold_pct=threshold,
            fee_buffer_pct=fee_buffer,
            merchant_check=merchant,
            rows=defaults.rows,
            outlier_max_dev_pct=defaults.outlier_max_dev_pct,
        )
        for asset in assets
    ]

    config = AppConfig(
        targets=targets,
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
        targets_lbl = ", ".join(t.label for t in config.targets)
        console.print(
            f"\n[bold green]Monitoreando[/] {targets_lbl} "
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
