"""Modelos puros del dominio.

Esta capa NO conoce curl_cffi, ni SQLite, ni rich. Solo dataclasses inmutables
y tipos. Es testeable de forma aislada, sin red ni disco.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

TradeType = Literal["BUY", "SELL"]

#: Plantilla del enlace al LIBRO P2P en vivo, filtrado por lado, par, fiat y
#: método de pago. No se enlaza a un anuncio concreto: el ``advNo`` que devuelve
#: el endpoint público viene redondeado (float64, pierde los dígitos bajos) y no
#: sirve para un deep-link por publicación; además los anuncios P2P son efímeros
#: y "expiran" en segundos. El libro filtrado siempre muestra los anuncios
#: vivos de ese lado/método, que es lo útil para actuar.
_BOOK_URL = "https://p2p.binance.com/es/trade/{side}/{asset}?fiat={fiat}&payment={payment}"

#: Valor del parámetro ``payment`` cuando no se filtra por método concreto.
_ALL_PAYMENTS = "all-payments"


def book_url(
    *, trade_type: TradeType, asset: str, fiat: str, pay_method: str = ""
) -> str:
    """Enlace al libro P2P en vivo para ese lado/par/fiat/método.

    ``trade_type`` es la perspectiva del usuario: ``"BUY"`` abre la pestaña
    Comprar; ``"SELL"``, la de Vender. ``pay_method`` vacío => todos los métodos.
    """
    side = "buy" if trade_type == "BUY" else "sell"
    payment = pay_method or _ALL_PAYMENTS
    return _BOOK_URL.format(side=side, asset=asset, fiat=fiat, payment=payment)


@dataclass(frozen=True, slots=True)
class Ad:
    """Un anuncio concreto del libro P2P.

    Los precios y límites están en la moneda fiat (p. ej. VES). ``trade_type``
    indica el lado desde la perspectiva del USUARIO del bot:

    - ``"BUY"``  -> anuncios donde el usuario *compra* USDT (precio que paga).
                    El mejor es el de precio MÁS BAJO.
    - ``"SELL"`` -> anuncios donde el usuario *vende* USDT (precio que recibe).
                    El mejor es el de precio MÁS ALTO.
    """

    adv_no: str
    price: Decimal
    surplus: Decimal              # surplusAmount: USDT disponible en el anuncio
    min_amount: Decimal           # minSingleTransAmount, en fiat
    max_amount: Decimal           # maxSingleTransAmount, en fiat
    pay_methods: tuple[str, ...]  # identifiers de tradeMethods
    advertiser_no: str            # userNo
    advertiser_name: str          # nickName
    trade_type: TradeType


@dataclass(frozen=True, slots=True)
class WatchTarget:
    """Un objetivo de vigilancia: qué par/fiat y con qué parámetros.

    El motor itera sobre una lista de estos, de modo que añadir más pares o
    fiats no requiere cambios estructurales (multi-par / multi-fiat).
    """

    asset: str
    fiat: str
    pay_methods: tuple[str, ...]   # identifiers a vigilar; vacío => "todos"
    max_usdt: Decimal
    threshold_pct: Decimal
    fee_buffer_pct: Decimal = Decimal("0")
    merchant_check: bool = False
    rows: int = 20                 # filas a pedir por lado/método
    # Desviación máxima (%) respecto a la mediana del libro para considerar válido
    # un anuncio. Descarta precios "cebo" atípicos. 0 (default) desactiva el filtro.
    outlier_max_dev_pct: Decimal = Decimal("0")

    @property
    def label(self) -> str:
        return f"{self.asset}/{self.fiat}"


@dataclass(frozen=True, slots=True)
class Opportunity:
    """Snapshot inmutable de una oportunidad detectada (para persistir/avisar)."""

    detected_at: datetime
    fiat: str
    asset: str
    buy_pay_method: str
    sell_pay_method: str
    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    net_pct: Decimal
    max_usdt: Decimal
    buy_adv_no: str
    sell_adv_no: str
    buy_advertiser: str
    sell_advertiser: str
    buy_url: str
    sell_url: str
    # Ganancia estimada bruta en fiat para max_usdt (informativo, no se persiste).
    est_profit_fiat: Decimal = field(default=Decimal("0"))
    # Ganancia neta estimada expresada en USDT (max_usdt * net_pct / 100). Se persiste.
    est_profit_usdt: Decimal = field(default=Decimal("0"))
    # Límites de transacción (en fiat) de los anuncios elegidos, para dimensionar el
    # monto a operar en el dashboard. minSingleTransAmount / maxSingleTransAmount.
    # Default 0 => "sin dato" (retrocompat con filas/registros antiguos). Se persisten.
    buy_min_amount: Decimal = field(default=Decimal("0"))
    buy_max_amount: Decimal = field(default=Decimal("0"))
    sell_min_amount: Decimal = field(default=Decimal("0"))
    sell_max_amount: Decimal = field(default=Decimal("0"))

    @property
    def dedup_key(self) -> tuple[str, str, str, str]:
        """Identidad para suprimir duplicados ruidosos en una ventana corta."""
        return (
            self.buy_adv_no,
            self.sell_adv_no,
            str(self.buy_price),
            str(self.sell_price),
        )


@dataclass(frozen=True, slots=True)
class TradeSizing:
    """Monto a operar en una oportunidad, acotado por fondo y límites de anuncio.

    ``usable`` es lo que conviene mover (el fondo, salvo que un máximo lo recorte);
    ``min_required`` es el piso impuesto por los mínimos de compra/venta. Si el fondo
    no alcanza el mínimo, ``feasible`` es ``False`` y ``usable_*`` quedan a 0.
    Los montos ``*_fiat`` están en la moneda fiat (VES); ``*_usdt`` en USDT.
    """

    usable_usdt: Decimal
    usable_fiat_buy: Decimal        # fiat que pagas al comprar usable_usdt
    usable_fiat_sell: Decimal       # fiat que recibes al vender usable_usdt
    min_required_usdt: Decimal
    min_required_fiat_buy: Decimal  # fiat mínimo a mover en la compra
    feasible: bool
