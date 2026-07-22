"""Adaptador ``Notifier`` que guarda una "ficha" PNG de cada oportunidad.

Las oportunidades en Binance P2P son efímeras: cuando el usuario abre el enlace al
libro en vivo, los anuncios ya suelen haber expirado y no puede confirmar que la
oportunidad existió. Este notifier deja en disco, en el **instante exacto** de la
detección, una imagen con los datos que el bot ya tiene (anuncios de compra y venta
ganadores, precios, comerciantes, métodos y la aritmética del spread). Es fiel al
segundo y no pega a Binance por su cuenta.

No usa navegador (coherente con el resto del repo, que solo consulta el endpoint
JSON con ``curl_cffi``): la imagen se genera con Pillow a partir del ``Opportunity``.
Se añade a la lista de notifiers de ``MonitorService`` junto al ``ConsoleNotifier``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from decimal import Decimal

from PIL import Image, ImageDraw, ImageFont

from ..domain.models import Opportunity, WatchTarget

logger = logging.getLogger(__name__)

# Fuentes candidatas con buena cobertura latina (acentos, ñ) para que los nombres
# de comerciantes se lean bien. La primera es la fuente empaquetada con el proyecto
# (garantiza buen render incluso en imágenes sin fuentes de sistema, p. ej. Docker
# slim, y sin depender de egress para instalarla). Si no está, se prueban fuentes
# de sistema y, en último caso, la fuente por defecto de Pillow.
_BUNDLED_FONT = os.path.join(os.path.dirname(__file__), "fonts", "LiberationSans-Regular.ttf")
_TTF_CANDIDATES = (
    _BUNDLED_FONT,
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",  # Fedora
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",  # Fedora
)

# Paleta sobria.
_BG = (18, 22, 28)
_FG = (230, 233, 238)
_MUTED = (150, 158, 168)
_BUY = (86, 204, 140)  # verde: lado COMPRA (lo que le interesa al usuario)
_SELL = (232, 168, 90)  # ámbar: lado VENTA
_LINE = (44, 50, 60)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Devuelve una fuente escalable; usa la por defecto de Pillow si no hay TTF."""
    for path in _TTF_CANDIDATES:
        with contextlib.suppress(OSError):
            return ImageFont.truetype(path, size)
    # Pillow >= 10.1 admite tamaño en la fuente por defecto (TrueType embebida).
    with contextlib.suppress(TypeError):
        return ImageFont.load_default(size=size)
    return ImageFont.load_default()


class ScreenshotNotifier:
    """Persiste una ficha PNG por cada oportunidad detectada."""

    def __init__(self, screenshots_dir: str = "screenshots", *, enabled: bool = True) -> None:
        self._dir = screenshots_dir
        self._enabled = enabled

    async def notify_heartbeat(
        self, target: WatchTarget, best_spread: Decimal | None
    ) -> None:
        # No hay nada que capturar en un heartbeat.
        return None

    async def notify_opportunity(self, opp: Opportunity) -> None:
        if not self._enabled:
            return None
        path = os.path.join(self._dir, self._filename(opp))
        try:
            # PIL es CPU-bound; lo sacamos del event loop para no frenar el ciclo.
            await asyncio.to_thread(self._render, opp, path)
        except Exception as exc:  # noqa: BLE001 — no debe tumbar el ciclo del bot
            logger.warning("No se pudo guardar la captura en %s: %s", path, exc)

    @staticmethod
    def _filename(opp: Opportunity) -> str:
        """``AAAAMMDD_HHMMSS_ASSET-FIAT_id.png`` (fecha local + id de la oportunidad)."""
        ts = opp.detected_at.astimezone().strftime("%Y%m%d_%H%M%S")
        shortid = hashlib.sha1(str(opp.dedup_key).encode()).hexdigest()[:8]
        return f"{ts}_{opp.asset}-{opp.fiat}_{shortid}.png"

    def _render(self, opp: Opportunity, path: str) -> None:
        """Dibuja la ficha y la escribe de forma atómica (tmp + os.replace)."""
        os.makedirs(self._dir, exist_ok=True)

        f_title = _font(30)
        f_head = _font(20)
        f_body = _font(18)
        f_small = _font(14)

        width = 780
        margin = 28
        img = Image.new("RGB", (width, 470), _BG)
        d = ImageDraw.Draw(img)
        x = margin
        y = margin

        local = opp.detected_at.astimezone()
        utc = opp.detected_at

        d.text((x, y), f"Oportunidad {opp.asset}/{opp.fiat}", font=f_title, fill=_FG)
        y += 40
        d.text(
            (x, y),
            f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')}  ·  UTC {utc.strftime('%H:%M:%S')}",
            font=f_small,
            fill=_MUTED,
        )
        y += 30
        d.line([(x, y), (width - margin, y)], fill=_LINE, width=1)
        y += 18

        # --- Lado COMPRA (lo que el usuario no alcanza a ver a tiempo) ----------
        y = self._leg(
            d, x, y, width - margin, "COMPRA", _BUY,
            advertiser=opp.buy_advertiser,
            price=opp.buy_price,
            fiat=opp.fiat,
            method=opp.buy_pay_method,
            adv_no=opp.buy_adv_no,
            url=opp.buy_url,
            f_head=f_head, f_body=f_body, f_small=f_small,
        )
        y += 8

        # --- Lado VENTA ---------------------------------------------------------
        y = self._leg(
            d, x, y, width - margin, "VENTA", _SELL,
            advertiser=opp.sell_advertiser,
            price=opp.sell_price,
            fiat=opp.fiat,
            method=opp.sell_pay_method,
            adv_no=opp.sell_adv_no,
            url=opp.sell_url,
            f_head=f_head, f_body=f_body, f_small=f_small,
        )
        y += 8
        d.line([(x, y), (width - margin, y)], fill=_LINE, width=1)
        y += 16

        # --- Aritmética ---------------------------------------------------------
        d.text(
            (x, y),
            f"Spread: {opp.spread_pct:.3f}%   Neto: {opp.net_pct:.3f}%",
            font=f_head,
            fill=_FG,
        )
        y += 28
        d.text(
            (x, y),
            f"Monto máx: {opp.max_usdt} {opp.asset}   ·   "
            f"Ganancia neta est.: {opp.est_profit_usdt} {opp.asset}",
            font=f_body,
            fill=_MUTED,
        )

        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                img.save(fh, format="PNG")
            os.replace(tmp, path)  # rename atómico
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @staticmethod
    def _leg(
        d: ImageDraw.ImageDraw,
        x: int,
        y: int,
        right: int,
        title: str,
        color: tuple[int, int, int],
        *,
        advertiser: str,
        price: Decimal,
        fiat: str,
        method: str,
        adv_no: str,
        url: str,
        f_head,
        f_body,
        f_small,
    ) -> int:
        """Dibuja un bloque de un lado (compra/venta) y devuelve la nueva ``y``."""
        d.rectangle([(x, y + 2), (x + 6, y + 22)], fill=color)
        d.text((x + 16, y), f"{title}   {price} {fiat}", font=f_head, fill=color)
        y += 30
        method_lbl = method if method and method != "ALL" else "todos los métodos"
        d.text(
            (x + 16, y),
            f"{advertiser or '(sin nombre)'}   ·   {method_lbl}",
            font=f_body,
            fill=_FG,
        )
        y += 26
        d.text((x + 16, y), f"advNo {adv_no}", font=f_small, fill=_MUTED)
        y += 20
        d.text((x + 16, y), url, font=f_small, fill=_MUTED)
        y += 24
        return y
