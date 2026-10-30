import { useState } from "react";
import { api, errorText, newKey } from "../api";
import { SIDE_LABEL, dateTime, money, orderStatus } from "../format";
import type { Mode, OrderPreview, Status } from "../types";

type Kind = "qty" | "notional";

interface SubmitResult {
  duplicate: boolean;
  uncertain?: boolean;
  order: { status: string; symbol: string; side: string; client_order_id: string };
}

export function OrderForm({ status, onDone }: { status: Status; onDone: () => void }) {
  const mode: Mode = status.mode;
  const [symbol, setSymbol] = useState("SPY");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [kind, setKind] = useState<Kind>("notional");
  const [amount, setAmount] = useState("10");
  const [tif, setTif] = useState<"day" | "gtc">("day");
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [key, setKey] = useState<string>(newKey());
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);

  const paperLocked = mode === "PAPER" && !status.orders_enabled;
  const crypto = symbol.includes("/");

  function changed() {
    setPreview(null);
    setConfirmed(false);
    setKey(newKey());
  }

  function body() {
    return {
      expected_mode: mode,
      symbol: symbol.trim().toUpperCase(),
      side,
      time_in_force: crypto ? "gtc" : tif,
      qty: kind === "qty" ? amount.replace(",", ".") : null,
      notional: kind === "notional" ? amount.replace(",", ".") : null,
    };
  }

  async function doPreview(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      setPreview(await api.post<OrderPreview>("/orders/preview", body()));
      setConfirmed(false);
    } catch (err) {
      setPreview(null);
      setMsg({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!preview) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await api.post<SubmitResult>("/orders", { ...body(), idempotency_key: key, confirm: true });
      const st = orderStatus(res.order.status);
      if (res.duplicate) {
        setMsg({ tone: "warn", text: "Diese Order wurde bereits gesendet (Doppelklick erkannt) – kein zweiter Auftrag." });
      } else if (res.uncertain || res.order.status === "local_unknown") {
        setMsg({
          tone: "warn",
          text: "Ergebnis unklar (Zeitüberschreitung). Der Brokerzustand wird abgeglichen; die Order wird NICHT automatisch erneut gesendet. Käufe sind bis zur Klärung gesperrt.",
        });
      } else {
        setMsg({
          tone: "ok",
          text: `${mode}-Order übermittelt: ${SIDE_LABEL[res.order.side]} ${res.order.symbol}, Status „${st.label}“. Angenommen bedeutet noch nicht ausgeführt – siehe Auftragstabelle.`,
        });
      }
      setPreview(null);
      setConfirmed(false);
      setKey(newKey());
      onDone();
    } catch (err) {
      setMsg({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  const cur = preview?.currency ?? null;
  return (
    <div className="card">
      <div className="card-head">
        <h2>Manuelle {mode === "PAPER" ? "Paper" : "Demo"}-Order</h2>
        <span className={`badge mode-${mode}`}>{mode === "PAPER" ? "PAPER-Konto (Spielgeld)" : "DEMO – simuliert"}</span>
      </div>

      {paperLocked && (
        <div className="notice warn" style={{ marginBottom: 14 }}>
          <strong>Paper-Orders sind gesperrt</strong>
          Die Paper-Orderfreigabe ist aus (Standard nach jedem Neustart). Sie können die Order prüfen, aber nicht senden.
          Freigabe unter „Einstellungen“.
        </div>
      )}

      <form onSubmit={doPreview} className="stack">
        <div className="form-grid">
          <label className="field">
            Symbol (US-Aktie/ETF oder Krypto wie BTC/USD)
            <input value={symbol} maxLength={14} onChange={(e) => { setSymbol(e.target.value.toUpperCase()); changed(); }} required />
          </label>
          <div className="field">
            <span className="small muted">Seite</span>
            <div className="segmented">
              <button type="button" className={side === "buy" ? "active" : ""} onClick={() => { setSide("buy"); changed(); }}>Kauf</button>
              <button type="button" className={side === "sell" ? "active" : ""}
                      onClick={() => { setSide("sell"); setKind("qty"); setTif("day"); changed(); }}>Verkauf</button>
            </div>
          </div>
          <div className="field">
            <span className="small muted">Angabe als</span>
            <div className="segmented">
              <button type="button" className={kind === "notional" ? "active" : ""} disabled={side === "sell"}
                      onClick={() => { setKind("notional"); setTif("day"); changed(); }}>Betrag</button>
              <button type="button" className={kind === "qty" ? "active" : ""} onClick={() => { setKind("qty"); changed(); }}>Stückzahl</button>
            </div>
          </div>
          <label className="field">
            {kind === "qty" ? "Stückzahl" : "Betrag (Kontowährung)"}
            <input inputMode="decimal" value={amount} onChange={(e) => { setAmount(e.target.value); changed(); }} required />
          </label>
          <label className="field">
            Gültigkeit
            {crypto ? (
              <select value="gtc" disabled><option value="gtc">gtc – Krypto (rund um die Uhr)</option></select>
            ) : (
              <select value={tif} onChange={(e) => { setTif(e.target.value as "day" | "gtc"); changed(); }}>
                <option value="day">day – bis Handelsschluss</option>
                <option value="gtc" disabled={kind === "notional"}>gtc – bis Widerruf (nur ganze Stück)</option>
              </select>
            )}
          </label>
        </div>
        <div className="small muted">
          Nur Market-Orders, nur Long. Aktien: Betrags- und Bruchstück-Orders nur mit „day“. Krypto: immer „gtc“,
          Kauf zum Brief-, Verkauf zum Geldkurs, Gebühr je Ausführung. Verkäufe reduzieren ausschliesslich vorhandenen,
          nicht gebundenen Bestand.
        </div>
        <div className="row">
          <button className="btn" type="submit" disabled={busy}>Prüfen &amp; Zusammenfassung</button>
        </div>
      </form>

      {preview && (
        <div className="stack" style={{ marginTop: 18 }}>
          <h3>Zusammenfassung vor dem Absenden</h3>
          <dl className="summary">
            <dt>Modus</dt><dd><span className={`badge mode-${preview.mode}`}>{preview.mode}</span></dd>
            <dt>Instrument</dt><dd>{preview.symbol} {preview.asset_name ? `– ${preview.asset_name}` : ""}</dd>
            <dt>Auftrag</dt>
            <dd>
              {SIDE_LABEL[preview.side]} · Market ·{" "}
              {preview.qty ? `${preview.qty} Stück` : `Betrag ${money(Number(preview.notional), cur)}`} · Gültigkeit {preview.time_in_force}
            </dd>
            <dt>Kursschätzung</dt>
            <dd>
              {money(preview.est_price, cur)} {preview.est_price_source ? `(${preview.est_price_source}, ${dateTime(preview.est_price_at)})` : ""}
            </dd>
            <dt>Geschätzter Wert</dt><dd>{money(preview.est_value, cur)}</dd>
            <dt>Markt</dt><dd>{preview.market_open === null ? "—" : preview.market_open ? "offen" : "geschlossen"}</dd>
          </dl>
          <ul className="checks" aria-label="Prüfungen">
            {preview.checks.map((c) => (
              <li key={c.name}>
                <span className={c.ok ? "tone-ok" : c.blocking ? "tone-bad" : "tone-warn"}>{c.ok ? "✓" : "✗"}</span>
                <span className="name">{c.name}</span>
                <span className="detail">{c.detail}</span>
              </li>
            ))}
          </ul>
          {preview.warnings.map((w) => (
            <div key={w} className="small tone-warn">{w}</div>
          ))}
          {preview.allowed ? (
            <>
              <label className="checkbox">
                <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
                <span>Ich habe die Zusammenfassung geprüft und möchte diese {preview.mode}-Order senden.</span>
              </label>
              <div className="row">
                <button className="btn primary" disabled={!confirmed || busy || paperLocked} onClick={submit}>
                  {preview.mode === "PAPER" ? "Paper-Order senden" : "Demo-Order senden"}
                </button>
                <button className="btn" onClick={changed} disabled={busy}>Verwerfen</button>
              </div>
            </>
          ) : (
            <div className="notice bad"><strong>Order gesperrt</strong>Mindestens eine Prüfung ist nicht erfüllt (siehe ✗).</div>
          )}
        </div>
      )}

      {msg && <div className={`notice ${msg.tone}`} style={{ marginTop: 14 }}>{msg.text}</div>}
    </div>
  );
}
