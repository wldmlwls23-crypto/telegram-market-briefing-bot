from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


AssetKind = Literal["crypto", "index", "fx", "commodity", "yield"]


class AssetQuote(BaseModel):
    key: str
    name_ko: str
    kind: AssetKind
    current: float
    previous: float | None = None
    absolute_change: float | None = None
    percent_change: float | None = None
    as_of: datetime
    market_state: str = "UNKNOWN"
    source: str
    comparison_label: str = "전일"
    stale: bool = False
    unit: str = ""


class EconomicEvent(BaseModel):
    event_id: str
    title: str
    title_ko: str
    country: str
    country_ko: str
    event_time_kst: datetime
    importance: Literal["★★★★", "★★★★★"]
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    sensitivity_stronger: str
    sensitivity_weaker: str
    source: str = "Forex Factory calendar"

    @property
    def value_summary(self) -> str:
        parts: list[str] = []
        if self.actual:
            parts.append(f"실제 {self.actual}")
        if self.forecast:
            parts.append(f"예상 {self.forecast}")
        if self.previous:
            parts.append(f"이전 {self.previous}")
        return " / ".join(parts)


class NewsItem(BaseModel):
    news_id: str
    topic_key: str
    title: str
    publisher: str
    published_at: datetime | None = None
    url: str
    summary: str = ""
    official_source: bool = False
    trusted_source: bool = False


class SignalSelection(BaseModel):
    candidate_id: str
    title_ko: str = Field(min_length=2, max_length=70)
    meaning: str = Field(min_length=5, max_length=140)
    related_asset_keys: list[str] = Field(default_factory=list, max_length=3)
    relation: Literal["원인 후보", "시장 배경", "엇갈림"] = "시장 배경"


class SensitivitySelection(BaseModel):
    event_id: str


class MorningAnalysis(BaseModel):
    signals: list[SignalSelection] = Field(default_factory=list, max_length=2)


class EmergencyAnalysis(BaseModel):
    verified: bool
    summary_ko: str = Field(min_length=5, max_length=240)
    meaning: str = Field(min_length=5, max_length=240)
    source_news_ids: list[str] = Field(default_factory=list, max_length=3)


class MarketData(BaseModel):
    generated_at_kst: datetime
    quotes: dict[str, AssetQuote]
    events: list[EconomicEvent]
    news: list[NewsItem]
    btc_series: "PriceSeries | None" = None
    errors: list[str] = Field(default_factory=list)


class PricePoint(BaseModel):
    timestamp: datetime
    value: float


class PriceSeries(BaseModel):
    key: str
    name: str
    points: list[PricePoint]
    source: str
