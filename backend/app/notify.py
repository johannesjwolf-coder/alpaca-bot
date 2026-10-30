"""Push-Benachrichtigungen über ntfy (optional).

Konfiguration ausschliesslich über Umgebungsvariablen auf dem Server, nie über
die Oberfläche:

* ``NTFY_TOPIC`` – Themenname. Wer ihn kennt, kann mitlesen: wie ein Passwort
  behandeln. Er wird weder geloggt noch über die API ausgegeben.
* ``NTFY_URL`` – Server, Standard ``https://ntfy.sh`` (nur https; http nur für
  einen eigenen Server auf localhost).
* ``NTFY_TOKEN`` – optionales Zugriffstoken für geschützte Themen.

Welche Ereignisse gemeldet werden, wird in der Oberfläche eingestellt. Die
Nachrichten enthalten Symbole, Kurse und Ergebnisse, aber nie Zugangsdaten.
Versandfehler werden nur gezählt – sie beeinflussen den Handel nie.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import _is_loopback
from .db import iso
from .settings_store import APP_SCOPE, NotifySettings, get_notify_settings
from .timeutil import utcnow

if TYPE_CHECKING:
    from .core import AppContext

log = logging.getLogger("alpaca_bot.notify")

DEFAULT_NTFY_URL = "https://ntfy.sh"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
RATE_PER_MIN = 12
DEDUP_S = 120
MAX_LEN = 1000
SUMMARY_KEY = "notify_summary_day"


class NotifyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NtfyConfig:
    url: str
    topic: str
    token: str | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    @classmethod
    def from_env(cls) -> NtfyConfig | None:
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        if not topic:
            return None
        url = (os.environ.get("NTFY_URL", "").strip() or DEFAULT_NTFY_URL).rstrip("/")
        return cls.validated(url, topic, os.environ.get("NTFY_TOKEN", "").strip() or None)

    @classmethod
    def validated(cls, url: str, topic: str, token: str | None) -> NtfyConfig:
        if not TOPIC_RE.match(topic):
            raise NotifyConfigError("NTFY_TOPIC darf nur Buchstaben, Ziffern, - und _ enthalten (max. 64 Zeichen).")
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.username or parts.password or parts.query or parts.fragment or not host:
            raise NotifyConfigError("NTFY_URL muss eine einfache Adresse sein, z. B. https://ntfy.sh")
        if parts.scheme != "https" and not (parts.scheme == "http" and _is_loopback(host)):
            raise NotifyConfigError("NTFY_URL muss https verwenden (http nur für localhost).")
        return cls(url=url, topic=topic, token=token)


def app_timezone() -> ZoneInfo:
    name = os.environ.get("APP_TIMEZONE", "").strip() or "Europe/Zurich"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Zurich")


def classify(level: str, category: str, push: bool) -> tuple[str, int, list[str]] | None:
    """Ereignis → (Art, ntfy-Priorität 1–5, Tags) oder None (nicht melden)."""
    if category == "trade":
        return ("trades", 3, ["chart_with_upwards_trend"] if level == "info" else ["chart_with_downwards_trend"])
    if level in ("error", "critical"):
        return ("warnings", 5 if level == "critical" else 4, ["rotating_light"])
    if level == "warning":
        return ("warnings", 4, ["warning"])
    if push and category in ("bot", "system"):
        return ("bot", 3, ["robot"] if category == "bot" else ["gear"])
    return None


class Notifier:
    def __init__(
        self,
        ctx: AppContext,
        config: NtfyConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.ctx = ctx
        self.config_error: str | None = None
        if config is None:
            try:
                config = NtfyConfig.from_env()
            except NotifyConfigError as exc:
                self.config_error = str(exc)
        self.config = config
        self._transport = transport
        self._now = now
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self._tasks: list[asyncio.Task] = []
        self._sent: deque[datetime] = deque()
        self._recent: dict[str, datetime] = {}
        self.dropped = 0
        self.sent_total = 0
        self.last_sent_at: datetime | None = None
        self.last_error: str | None = None
        self.last_test_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return self.config is not None

    def settings(self) -> NotifySettings:
        return get_notify_settings(self.ctx.db)

    def describe(self) -> dict:
        """Status für die Oberfläche – ohne Thema und Token."""
        return {
            "configured": self.configured,
            "server": self.config.host if self.config else None,
            "token_set": bool(self.config and self.config.token),
            "config_error": self.config_error,
            "timezone": app_timezone().key,
            "settings": self.settings().model_dump(),
            "sent_total": self.sent_total,
            "dropped": self.dropped,
            "last_sent_at": iso(self.last_sent_at),
            "last_error": self.last_error,
        }

    # ---------------------------------------------------------- Ereignisse
    def on_event(self, mode: str, level: str, category: str, message: str, push: bool) -> None:
        if not self.configured:
            return
        s = self.settings()
        if not s.enabled:
            return
        kind = classify(level, category, push)
        if kind is None:
            return
        what, priority, tags = kind
        if not getattr(s, what):
            return
        if s.paper_only and mode != "PAPER" and category != "system":
            return
        self.enqueue(f"{mode} · {self._title(what, level)}", message, priority, tags)

    @staticmethod
    def _title(what: str, level: str) -> str:
        if what == "trades":
            return "Trade"
        if what == "warnings":
            return "Fehler" if level in ("error", "critical") else "Warnung"
        return "Automatik"

    def enqueue(self, title: str, message: str, priority: int = 3, tags: list[str] | None = None) -> bool:
        if not self.configured:
            return False
        now = self._now()
        key = f"{title}|{message}"
        last = self._recent.get(key)
        if last is not None and (now - last).total_seconds() < DEDUP_S:
            return False
        while self._sent and (now - self._sent[0]).total_seconds() >= 60:
            self._sent.popleft()
        if len(self._sent) >= RATE_PER_MIN:
            self.dropped += 1
            return False
        self._recent = {k: t for k, t in self._recent.items() if (now - t).total_seconds() < DEDUP_S}
        self._recent[key] = now
        self._sent.append(now)
        text = message[:MAX_LEN]
        if self.dropped:
            text += f"\n(+{self.dropped} weitere Meldung(en) wegen Ratenbegrenzung unterdrückt)"
        try:
            self._queue.put_nowait({"title": title, "message": text, "priority": priority, "tags": tags or []})
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self.dropped = 0
        return True

    # ------------------------------------------------------------- Versand
    def _client(self) -> httpx.AsyncClient:
        # trust_env behält Proxy/Zertifikats-Einstellungen; keine Weiterleitungen.
        return httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=True, transport=self._transport)

    async def _post(self, client: httpx.AsyncClient, item: dict) -> str | None:
        """Sendet eine Nachricht; gibt eine Fehlerbeschreibung (ohne Geheimnisse) oder None zurück."""
        assert self.config is not None
        headers = {"Authorization": f"Bearer {self.config.token}"} if self.config.token else {}
        payload = {"topic": self.config.topic, **item}
        try:
            resp = await client.post(self.config.url + "/", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return f"Netzwerkfehler ({type(exc).__name__})"
        if resp.status_code >= 300:
            return f"ntfy antwortete mit HTTP {resp.status_code}"
        return None

    async def _deliver(self, client: httpx.AsyncClient, item: dict) -> str | None:
        error = await self._post(client, item)
        if error is None:
            self.sent_total += 1
            self.last_sent_at = self._now()
            self.last_error = None
        else:
            self.last_error = error
            log.warning("Benachrichtigung nicht zugestellt: %s", error)
        return error

    async def _worker(self) -> None:
        async with self._client() as client:
            while True:
                await self._deliver(client, await self._queue.get())

    async def flush(self) -> None:
        """Wartende Nachrichten sofort senden (Tests und Herunterfahren)."""
        if not self.configured:
            return
        async with self._client() as client:
            while not self._queue.empty():
                await self._deliver(client, self._queue.get_nowait())

    async def send_test(self) -> dict:
        if not self.configured:
            return {"ok": False, "error": self.config_error or "NTFY_TOPIC ist auf dem Server nicht gesetzt."}
        async with self._client() as client:
            error = await self._deliver(client, {
                "title": "Alpaca-Bot · Test",
                "message": f"Testnachricht vom Alpaca Paper-Bot ({self.ctx.mode}). Benachrichtigungen funktionieren.",
                "priority": 3, "tags": ["white_check_mark"],
            })
        return {"ok": error is None, "error": error}

    # ------------------------------------------------- Tageszusammenfassung
    def summary_due(self) -> str | None:
        """Lokales Datum, wenn die Zusammenfassung jetzt fällig ist."""
        s = self.settings()
        if not (self.configured and s.enabled and s.daily_summary):
            return None
        local = self._now().astimezone(app_timezone())
        hh, mm = (int(x) for x in s.summary_time.split(":"))
        if (local.hour, local.minute) < (hh, mm):
            return None
        day = local.date().isoformat()
        return None if self.ctx.db.get(APP_SCOPE, SUMMARY_KEY) == day else day

    def summary_text(self, mode: str) -> str:
        sm = self.ctx.bot.scalper.summary(mode)
        lines = [
            f"Bot-Trades heute: {sm['trades_today']} ({sm['wins_today']} Gewinner, {sm['losses_today']} Verlierer)",
            f"Netto geschätzt: {sm['net_today']:+.2f} USD",
        ]
        snap = self.ctx.runtimes[mode].snapshot
        if snap is not None:
            acct = snap.account
            lines.append(f"Kontowert: {acct.equity:.2f} {acct.currency}")
            if acct.last_equity:
                lines.append(f"Tagesergebnis Konto: {acct.equity - acct.last_equity:+.2f} {acct.currency}")
            lines.append(f"Offene Positionen: {len(snap.positions)}")
        bot = self.ctx.bot
        lines.append(f"Automatik: {'läuft' if bot.running and bot.mode == mode else 'pausiert'}")
        return "\n".join(lines)

    def maybe_send_summary(self) -> bool:
        day = self.summary_due()
        if day is None:
            return False
        mode = "PAPER" if self.settings().paper_only else self.ctx.mode
        self.ctx.db.set(APP_SCOPE, SUMMARY_KEY, day)
        return self.enqueue(f"{mode} · Tageszusammenfassung {day}", self.summary_text(mode), 2, ["bar_chart"])

    async def _summary_loop(self) -> None:
        while True:
            try:
                self.maybe_send_summary()
            except Exception:  # noqa: BLE001
                log.exception("Tageszusammenfassung fehlgeschlagen")
            await asyncio.sleep(30)

    # -------------------------------------------------------- Lebenszyklus
    def start(self) -> None:
        if not self.configured or self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker(), name="notify-worker"),
            asyncio.create_task(self._summary_loop(), name="notify-summary"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        try:
            await asyncio.wait_for(self.flush(), timeout=5)
        except Exception:  # noqa: BLE001 – Herunterfahren nie blockieren
            pass

