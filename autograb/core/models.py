"""Non-sensitive product records shared by parsing, storage and notification."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    availability: str
    prices: list[dict[str, Any]]
    product_url: str
    categories: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    eligible: bool = False
    order_url: str | None = None
    provider: str = "bandwagon"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Product":
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})
