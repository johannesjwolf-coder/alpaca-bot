"""Risikogrenzen und Strategieparameter je Betriebsart (in SQLite)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .broker.base import SYMBOL_RE, is_crypto
from .db import Database


class RiskLimits(BaseModel):
    """Technische Paper-Testwerte – bewusst klein, keine Anlageempfehlung."""

    max_order_value: float = Field(50.0, gt=0, le=100_000, description="Max. Wert je Order")
    max_total_invested: float = Field(200.0, gt=0, le=1_000_000, description="Max. investierter Gesamtbetrag")
    max_open_positions: int = Field(3, ge=1, le=50)
    max_orders_per_day: int = Field(100, ge=1, le=1000, description="Alle Orders (Käufe und Verkäufe)")
    max_buys_per_hour: int = Field(10, ge=1, le=500, description="Einstiege pro Stunde")
    max_buys_per_day: int = Field(50, ge=1, le=1000, description="Einstiege pro Handelstag")
    max_daily_loss: float = Field(25.0, gt=0, le=100_000, description="Max. Tagesverlust inkl. unrealisiert")
    loss_streak_limit: int = Field(3, ge=1, le=50, description="Verlust-Trades in Folge bis zur Pause")
    loss_pause_min: int = Field(30, ge=1, le=1440, description="Kaufpause nach Verlustserie (Minuten)")
    max_spread_pct: float = Field(0.1, gt=0, le=5, description="Kein Einstieg bei grösserem Spread (Krypto)")
    price_buffer_pct: float = Field(2.0, ge=0, le=20, description="Sicherheitsaufschlag für Market-Orders in %")


STRATEGIES = ("scalp", "sma")


class BotParams(BaseModel):
    """Parameter der Teststrategien. Keine validierte oder profitable Strategie.

    * ``scalp`` (Krypto): Kurse alle paar Sekunden, Mikrokerzen von ``bar_seconds``,
      Einstieg bei Aufwärtskreuzung kurzer/langer Durchschnitt, Ausstieg per
      Stop-Loss, Take-Profit oder maximaler Haltedauer.
    * ``sma`` (US-Aktien/ETFs): Kreuzung auf abgeschlossenen Minutenkerzen der laufenden Sitzung.
    """

    strategy: str = "scalp"
    symbol: str = "BTC/USD"  # Minutenstrategie (Aktie) bzw. erstes Scalping-Symbol
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"], description="Scalping-Paare")
    short_window: int = Field(6, ge=2, le=200)
    long_window: int = Field(18, ge=3, le=400)
    order_value: float = Field(50.0, gt=0, le=100_000, description="Zielwert je Bot-Kauf")
    max_bar_age_s: int = Field(180, ge=60, le=3600, description="Max. Alter der letzten Minutenkerze (sma)")
    bar_seconds: int = Field(10, ge=5, le=300, description="Länge einer Mikrokerze (scalp)")
    poll_interval_s: float = Field(2.0, ge=1.0, le=30.0, description="Kursabruf-Intervall (scalp)")
    stop_loss_pct: float = Field(0.3, gt=0, le=20)
    take_profit_pct: float = Field(0.6, gt=0, le=50)
    max_hold_min: float = Field(10.0, gt=0, le=1440)
    fee_pct: float = Field(0.25, ge=0, le=2, description="Geschätzte Gebühr je Ausführung in %")
    broker_stop: bool = Field(True, description="Stop-Limit-Schutzorder beim Broker nach jedem Kauf")
    stop_limit_offset_pct: float = Field(0.5, ge=0.05, le=5, description="Limit unter dem Stoppreis in %")

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v: str) -> str:
        if v not in STRATEGIES:
            raise ValueError("Strategie muss 'scalp' oder 'sma' sein")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not SYMBOL_RE.match(v):
            raise ValueError("Ungültiges Symbol")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            s = s.strip().upper()
            if not SYMBOL_RE.match(s):
                raise ValueError(f"Ungültiges Symbol: {s}")
            if s not in out:
                out.append(s)
        if len(out) > 5:
            raise ValueError("Höchstens 5 Scalping-Symbole gleichzeitig")
        return out

    @property
    def scalp_symbols(self) -> list[str]:
        return self.symbols or [self.symbol]

    @model_validator(mode="after")
    def _windows(self) -> "BotParams":
        if self.short_window >= self.long_window:
            raise ValueError("Kurzer Durchschnitt muss kleiner als der lange sein")
        if self.strategy == "scalp" and any(not is_crypto(s) for s in self.scalp_symbols):
            raise ValueError("Scalping ist in dieser Version nur für Krypto (z. B. BTC/USD) vorgesehen")
        if self.strategy == "scalp" and not is_crypto(self.symbol):
            raise ValueError("Scalping ist in dieser Version nur für Krypto (z. B. BTC/USD) vorgesehen")
        if self.strategy == "sma" and is_crypto(self.symbol):
            raise ValueError("Die SMA-Minutenstrategie ist für US-Aktien/ETFs vorgesehen")
        return self

    @property
    def roundtrip_cost_pct(self) -> float:
        return 2 * self.fee_pct


def get_risk_limits(db: Database, mode: str) -> RiskLimits:
    return RiskLimits(**db.get(mode, "risk_limits", {}))


def set_risk_limits(db: Database, mode: str, limits: RiskLimits) -> None:
    db.set(mode, "risk_limits", limits.model_dump())


def get_bot_params(db: Database, mode: str) -> BotParams:
    return BotParams(**db.get(mode, "bot_params", {}))


def set_bot_params(db: Database, mode: str, params: BotParams) -> None:
    db.set(mode, "bot_params", params.model_dump())


# Anwendungsweite Einstellungen (nicht je Modus)
APP_SCOPE = "APP"


class NotifySettings(BaseModel):
    """Welche Ereignisse als Push-Benachrichtigung (ntfy) verschickt werden."""

    enabled: bool = True
    paper_only: bool = Field(True, description="DEMO-Ereignisse nicht melden")
    trades: bool = Field(True, description="Einstiege und Ausstiege")
    warnings: bool = Field(True, description="Warnungen, Fehler, Risikosperren, Verbindungsprobleme")
    bot: bool = Field(True, description="Bot gestartet/pausiert, Neustart der Anwendung")
    daily_summary: bool = Field(True, description="Tageszusammenfassung")
    summary_time: str = Field("22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Uhrzeit (APP_TIMEZONE)")


def get_notify_settings(db: Database) -> NotifySettings:
    return NotifySettings(**db.get(APP_SCOPE, "notify", {}))


def set_notify_settings(db: Database, value: NotifySettings) -> None:
    db.set(APP_SCOPE, "notify", value.model_dump())
