import type { Status } from "../types";

export function connectionBadge(conn: Status["connection"]["connection"]): { label: string; tone: string } {
  switch (conn) {
    case "ok":
      return { label: "Verbunden", tone: "ok" };
    case "disconnected":
      return { label: "Getrennt", tone: "warn" };
    case "error":
      return { label: "Fehler", tone: "bad" };
    default:
      return { label: "Verbindung unbekannt", tone: "muted" };
  }
}

export function StatusBadges({ status, backendError }: { status: Status | null; backendError: string | null }) {
  if (backendError && !status) {
    return (
      <div className="badges">
        <span className="badge bad">
          <span className="dot" /> Backend nicht erreichbar
        </span>
      </div>
    );
  }
  if (!status) return <div className="badges muted small">Lade Status…</div>;
  const conn = connectionBadge(status.connection.connection);
  return (
    <div className="badges" aria-label="Status">
      <span className={`badge mode-${status.mode}`} title="Aktive Betriebsart">
        {status.mode}
      </span>
      <span
        className={`badge ${status.bot.running ? "ok running" : "muted"}`}
        title={status.bot.pause_meaning}
      >
        <span className="dot" /> Bot {status.bot.running ? "läuft" : "pausiert"}
      </span>
      <span className={`badge ${conn.tone}`} title={status.connection.last_error?.message ?? ""}>
        <span className="dot" /> {conn.label}
      </span>
      {status.mode === "PAPER" && (
        <span className={`badge ${status.orders_enabled ? "warn" : "muted"}`}>
          Paper-Orders {status.orders_enabled ? "freigegeben" : "gesperrt"}
        </span>
      )}
      {status.market && (
        <span className={`badge ${status.market.is_open ? "info" : "muted"}`}>
          Markt {status.market.is_open ? "offen" : "geschlossen"}
          {status.market.simulated ? " (simuliert)" : ""}
        </span>
      )}
      {backendError && <span className="badge bad">Aktualisierung fehlgeschlagen</span>}
    </div>
  );
}
