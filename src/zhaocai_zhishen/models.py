from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BidDocument:
    dataset_project: str
    file: str
    folder: str
    bidder: str
    project: str
    tender_no: str
    bid_date: str
    price: float | None
    device_price: float | None
    service_price: float | None
    install_price: float | None
    contact: str
    phone: str
    address: str
    capital: str
    founded: str
    staff: str
    warranty: str
    negative_deviations: int
    neutral_deviations: int
    table_count: int
    paragraph_count: int
    author: str
    last_modified_by: str
    created: str
    modified: str
    revision: str
    references: list[str] = field(default_factory=list)
