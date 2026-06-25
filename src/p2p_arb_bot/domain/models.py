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

#: Plantilla del enlace público al perfil del anunciante en Binance P2P.
_ADVERTISER_URL = "https://p2p.binance.com/en/advertiserDetail?advertiserNo={no}"


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

    @property
    def advertiser_url(self) -> str:
        return _ADVERTISER_URL.format(no=self.advertiser_no)


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

    @property
    def dedup_key(self) -> tuple[str, str, str, str]:
        """Identidad para suprimir duplicados ruidosos en una ventana corta."""
        return (
            self.buy_adv_no,
            self.sell_adv_no,
            str(self.buy_price),
            str(self.sell_price),
        )
