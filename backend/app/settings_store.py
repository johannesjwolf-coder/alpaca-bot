"""Risikogrenzen und Strategieparameter je Betriebsart (in SQLite)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .broker.base import SYMBOL_RE
from .db import Database


class RiskLimits(BaseModel):
    """Technische Paper-Testwerte – bewusst klein, keine Anlageempfehlung."""

    max_order_value: float = Field(50.0, gt=0, le=100_000, description="Max. Wert je Order")
    max_total_invested: float = Field(200.0, gt=0, le=1_000_000, description="Max. investierter Gesamtbetrag")
    max_open_positions: int = Field(3, ge=1, le=50)
    max_orders_per_day: int = Field(10, ge=1, le=500)
    max_daily_loss: float = Field(25.0, gt=0, le=100_000, description="Max. Tagesverlust inkl. unrealisiert")
    price_buffer_pct: float = Field(2.0, ge=0, le=20, description="Sicherheitsaufschlag für Market-Orders in %")


class BotParams(BaseModel):
    """Demo-Parameter der Beispielstrategie (SMA-Kreuzung). Keine validierte Strategie."""

    symbol: str = "SPY"
    short_window: int = Field(10, ge=2, le=200)
    long_window: int = Field(30, ge=3, le=400)
    order_value: float = Field(20.0, gt=0, le=100_000, description="Zielwert je Bot-Kauf")
    max_bar_age_s: int = Field(180, ge=60, le=3600, description="Max. Alter der letzten Kerze")

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not SYMBOL_RE.match(v):
            raise ValueError("Ungültiges Symbol")
        return v

    @model_validator(mode="after")
    def _windows(self) -> "BotParams":
        if self.short_window >= self.long_window:
            raise ValueError("Kurzer Durchschnitt muss kleiner als der lange sein")
        return self


def get_risk_limits(db: Database, mode: str) -> RiskLimits:
    return RiskLimits(**db.get(mode, "risk_limits", {}))


def set_risk_limits(db: Database, mode: str, limits: RiskLimits) -> None:
    db.set(mode, "risk_limits", limits.model_dump())


def get_bot_params(db: Database, mode: str) -> BotParams:
    return BotParams(**db.get(mode, "bot_params", {}))


def set_bot_params(db: Database, mode: str, params: BotParams) -> None:
    db.set(mode, "bot_params", params.model_dump())
