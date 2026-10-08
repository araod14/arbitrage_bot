"""Snapshots JSON con importes Decimal serializados como texto."""

from dataclasses import asdict
from decimal import Decimal
import json

from ..domain.models import Ad, WatchTarget
from ..domain.market import Route


def dumps(value: object) -> str:
    return json.dumps(value, default=lambda v: str(v) if isinstance(v, Decimal) else asdict(v),
                      ensure_ascii=False, sort_keys=True)


def target_dict(target: WatchTarget) -> dict:
    return json.loads(dumps(target))


def read_target(data: dict) -> WatchTarget:
    fields = dict(data)
    for key in ("max_fiat", "threshold_pct", "fee_buffer_pct", "outlier_max_dev_pct"):
        fields[key] = Decimal(str(fields[key]))
    fields["pay_methods"] = tuple(fields["pay_methods"])
    return WatchTarget(**fields)


def read_ads(data: list[dict]) -> list[Ad]:
    ads = []
    for row in data:
        fields = dict(row)
        for key in ("price", "surplus", "min_amount", "max_amount"):
            fields[key] = Decimal(str(fields[key]))
        fields["pay_methods"] = tuple(fields["pay_methods"])
        ads.append(Ad(**fields))
    return ads


def read_route(data: dict) -> Route:
    fields = dict(data)
    fields["buy"] = read_ads([fields["buy"]])[0]
    fields["sell"] = read_ads([fields["sell"]])[0]
    for key in ("net_pct", "units", "minimum_fiat"):
        fields[key] = Decimal(str(fields[key]))
    return Route(**fields)
