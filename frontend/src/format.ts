const LOCALE = "de-CH";

export function money(value: number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (!currency) return num(value, 2);
  return new Intl.NumberFormat(LOCALE, { style: "currency", currency, currencyDisplay: "code" }).format(value);
}

export function signedMoney(value: number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const s = money(Math.abs(value), currency);
  return value > 0 ? `+${s}` : value < 0 ? `−${s}` : s;
}

/** Betrag mit genau zwei Nachkommastellen (ohne Währung). */
export function amount2(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

/** Kursformat mit sinnvoller Genauigkeit je nach Grössenordnung. */
export function priceDigits(value: number): number {
  const v = Math.abs(value);
  if (v >= 1000) return 2;
  if (v >= 1) return 2;
  if (v >= 0.01) return 4;
  return 6;
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const d = priceDigits(value);
  return new Intl.NumberFormat(LOCALE, { minimumFractionDigits: d, maximumFractionDigits: d }).format(value);
}

export function pct(value: number | null | undefined, digits = 2, signed = true): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const s = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Math.abs(value));
  return `${signed ? (value > 0 ? "+" : value < 0 ? "−" : "") : ""}${s} %`;
}

export function tone(value: number | null | undefined): string {
  if (!value) return "";
  return value > 0 ? "pos" : "neg";
}

export function num(value: number | null | undefined, maxDigits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: maxDigits }).format(value);
}

// dateStyle/timeStyle lassen sich nicht mit timeZoneName kombinieren → Felder einzeln.
const DATE_TIME = new Intl.DateTimeFormat(LOCALE, {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  timeZoneName: "short",
});
const TIME = new Intl.DateTimeFormat(LOCALE, { hour: "2-digit", minute: "2-digit" });
const TZ = new Intl.DateTimeFormat(LOCALE, { timeZoneName: "short" })
  .formatToParts(new Date())
  .find((p) => p.type === "timeZoneName")?.value;

/** Zeitstempel immer mit Zeitzone anzeigen (lokale Zeit des Browsers). */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : DATE_TIME.format(d);
}

const SHORT_DT = new Intl.DateTimeFormat(LOCALE, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

/** Kurze Zeitangabe für Tabellen (Zeitzone steht in der Spaltenüberschrift). */
export function shortDT(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : SHORT_DT.format(d);
}

export function timeOnly(iso: string | Date): string {
  return TIME.format(typeof iso === "string" ? new Date(iso) : iso);
}

export const localTz = TZ ?? "lokale Zeit";

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "nie";
  const s = Math.round((now - new Date(iso).getTime()) / 1000);
  if (s < 5) return "gerade eben";
  if (s < 60) return `vor ${s} s`;
  if (s < 3600) return `vor ${Math.round(s / 60)} min`;
  return `vor ${Math.round(s / 3600)} h`;
}

const ORDER_STATUS: Record<string, [string, string]> = {
  new: ["Neu beim Broker", "info"],
  accepted: ["Angenommen", "info"],
  pending_new: ["Wird angelegt", "info"],
  accepted_for_bidding: ["Angenommen", "info"],
  partially_filled: ["Teilweise ausgeführt", "warn"],
  filled: ["Ausgeführt", "ok"],
  done_for_day: ["Für heute beendet", "muted"],
  canceled: ["Storniert", "muted"],
  expired: ["Verfallen", "muted"],
  replaced: ["Ersetzt", "muted"],
  pending_cancel: ["Storno läuft", "warn"],
  pending_replace: ["Änderung läuft", "warn"],
  rejected: ["Abgelehnt", "bad"],
  suspended: ["Ausgesetzt", "warn"],
  held: ["Zurückgehalten", "warn"],
  stopped: ["Gestoppt", "warn"],
  calculated: ["Berechnet", "info"],
  local_pending: ["Wird gesendet", "warn"],
  local_unknown: ["Ergebnis unklar – Abgleich", "bad"],
  local_failed: ["Vom Broker abgelehnt", "bad"],
  local_not_received: ["Nicht beim Broker angekommen", "muted"],
};

export function orderStatus(status: string): { label: string; tone: string } {
  const [label, tone] = ORDER_STATUS[status] ?? [status, "muted"];
  return { label, tone };
}

export const SIDE_LABEL: Record<string, string> = { buy: "Kauf", sell: "Verkauf" };
export const SOURCE_LABEL: Record<string, string> = { manual: "Manuell", bot: "Bot", extern: "Extern" };
