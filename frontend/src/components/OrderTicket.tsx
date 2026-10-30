import { useEffect, useState } from "react";
import { api, errorText, newKey } from "../api";
import { SIDE_LABEL, dateTime, money, num, orderStatus, price } from "../format";
import type { Mode, OrderPreview, Status } from "../types";
import { kindLabel, SymbolPicker } from "./SymbolPicker";
import { Icon, Notice, Seg, Spinner, useConfirm } from "./ui";

type Kind = "notional" | "qty";
const VERB = { buy: "Kaufen", sell: "Verkaufen" } as const;
interface SubmitResult {
  duplicate: boolean;
  uncertain?: boolean;
  order: { status: string; symbol: string; side: string; client_order_id: string };
}

function validate(amount: string, kind: Kind): string | null {
  const v = amount.trim().replace(",", ".");
  if (!v) return "Bitte einen Wert eingeben.";
  if (v.startsWith("-")) return "Der Wert muss grösser als 0 sein.";
  if (!/^\d+(\.\d+)?$/.test(v)) return "Nur Zahlen mit Punkt oder Komma.";
  const n = Number(v);
  if (n <= 0) return "Der Wert muss grösser als 0 sein.";
  const decimals = v.includes(".") ? v.split(".")[1].length : 0;
  if (kind === "notional" && decimals > 2) return "Beträge höchstens mit 2 Nachkommastellen.";
  if (kind === "notional" && n < 1) return "Mindestbetrag 1.";
  if (kind === "qty" && decimals > 9) return "Mengen höchstens mit 9 Nachkommastellen.";
  return null;
}

export function OrderTicket({ status, symbol, onSymbol, onDone, variant = "compact", onNavigate }: {
  status: Status; symbol: string; onSymbol: (s: string) => void; onDone: () => void;
  variant?: "compact" | "full"; onNavigate: (page: string) => void;
}) {
  const mode: Mode = status.mode;
  const confirm = useConfirm();
  const crypto = symbol.includes("/");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [kind, setKind] = useState<Kind>("notional");
  const [amount, setAmount] = useState("25");
  const [tif, setTif] = useState<"day" | "gtc">("day");
  const [touched, setTouched] = useState(false);
  const [preview, setPreview] = useState<OrderPreview | null>(null);
  const [key, setKey] = useState(newKey());
  const [busy, setBusy] = useState<"preview" | "submit" | null>(null);
  const [msg, setMsg] = useState<{ tone: string; text: string } | null>(null);
  const locked = mode === "PAPER" && !status.orders_enabled;
  const effKind: Kind = side === "sell" ? "qty" : kind;
  const error = touched ? validate(amount, effKind) : null;

  const reset = () => {
    setPreview(null);
    setKey(newKey());
  };
  useEffect(reset, [symbol, mode]);

  const body = () => ({
    expected_mode: mode,
    symbol,
    side,
    time_in_force: crypto ? "gtc" : effKind === "notional" ? "day" : tif,
    qty: effKind === "qty" ? amount.replace(",", ".") : null,
    notional: effKind === "notional" ? amount.replace(",", ".") : null,
  });

  async function doPreview(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (validate(amount, effKind)) return;
    setBusy("preview");
    setMsg(null);
    try {
      setPreview(await api.post<OrderPreview>("/orders/preview", body()));
    } catch (err) {
      setPreview(null);
      setMsg({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(null);
    }
  }

  const amountText = effKind === "notional" ? money(Number(amount.replace(",", ".")), preview?.currency ?? "USD") : `${amount} Stück`;

  async function submit() {
    if (!preview) return;
    const ok = await confirm({
      title: `${mode}-Order bestätigen`,
      tone: side === "buy" ? "buy" : "sell",
      confirmLabel: `Jetzt ${VERB[side].toLowerCase()}: ${symbol}`,
      body: (
        <>
          <dl className="kv">
            <dt>Modus</dt><dd><strong>{mode === "PAPER" ? "PAPER – Alpaca-Paper-Konto (Spielgeld)" : "DEMO – simuliert"}</strong></dd>
            <dt>Auftrag</dt><dd className={side === "buy" ? "side-buy" : "side-sell"}>{SIDE_LABEL[side]} · Market</dd>
            <dt>Symbol</dt><dd>{symbol} ({kindLabel(symbol)})</dd>
            <dt>{effKind === "notional" ? "Betrag" : "Menge"}</dt><dd>{amountText}</dd>
            <dt>Geschätzter Wert</dt><dd>{money(preview.est_value, preview.currency)}</dd>
            <dt>Gültigkeit</dt><dd>{preview.time_in_force}</dd>
          </dl>
          <p className="muted">Eine angenommene Order ist noch kein ausgeführter Trade. Der Status wird laufend abgeglichen.</p>
        </>
      ),
    });
    if (!ok) return;
    setBusy("submit");
    setMsg(null);
    try {
      const res = await api.post<SubmitResult>("/orders", { ...body(), idempotency_key: key, confirm: true });
      const st = orderStatus(res.order.status);
      if (res.duplicate) setMsg({ tone: "warn", text: "Diese Order wurde bereits gesendet (Doppelklick erkannt) – kein zweiter Auftrag." });
      else if (res.uncertain || res.order.status === "local_unknown")
        setMsg({ tone: "warn", text: "Ergebnis unklar (Zeitüberschreitung). Der Brokerzustand wird abgeglichen; keine automatische Wiederholung. Käufe bis zur Klärung gesperrt." });
      else setMsg({ tone: "ok", text: `${SIDE_LABEL[res.order.side]} ${res.order.symbol} übermittelt – Status „${st.label}“. Angenommen heisst noch nicht ausgeführt.` });
      reset();
      onDone();
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(null);
    }
  }

  const failed = preview?.checks.filter((c) => c.blocking && !c.ok) ?? [];
  const full = variant === "full";

  return (
    <section className="card" aria-label="Order-Ticket">
      <div className="card-head">
        <h2>{full ? "Manuelle Order" : "Schnellorder"}</h2>
        <span className="hint">{mode === "PAPER" ? "Paper-Konto" : "DEMO – simuliert"}</span>
      </div>
      <form className="card-body stack" onSubmit={doPreview} noValidate>
        {locked && (
          <Notice tone="warn" title="Paper-Orders sind gesperrt"
                  actions={<button type="button" className="btn sm" onClick={() => onNavigate("settings")}>Zur Freigabe</button>}>
            Die Paper-Orderfreigabe ist aus (Standard nach jedem Start). Prüfen ist möglich, Senden nicht.
          </Notice>
        )}
        <div className="field">
          <span>Symbol</span>
          <SymbolPicker value={symbol} onChange={onSymbol} mode={mode} label="Symbol für Order" />
        </div>
        <Seg label="Seite" className="block buy-sell" value={side}
             onChange={(v) => { setSide(v); reset(); }}
             options={[{ id: "buy", label: "Kaufen", side: "buy" }, { id: "sell", label: "Verkaufen", side: "sell" }]} />
        <div className="form-grid">
          <div className="field">
            <span>Angabe als</span>
            <Seg label="Angabe als" className="block" value={effKind} onChange={(v) => { setKind(v); reset(); }}
                 options={[
                   { id: "notional", label: "Betrag", disabled: side === "sell", title: side === "sell" ? "Verkäufe als Stückzahl" : undefined },
                   { id: "qty", label: "Stückzahl" },
                 ]} />
          </div>
          <label className={`field ${error ? "invalid" : ""}`}>
            <span>{effKind === "notional" ? "Betrag (USD)" : "Menge"}</span>
            <input inputMode="decimal" value={amount} aria-invalid={!!error} aria-describedby="amount-err"
                   onChange={(e) => { setAmount(e.target.value); reset(); }} onBlur={() => setTouched(true)} />
            {error && <span id="amount-err" className="field-error">{error}</span>}
          </label>
          {full && (
            <label className="field">
              <span>Gültigkeit</span>
              {crypto ? (
                <select value="gtc" disabled><option value="gtc">gtc – Krypto, rund um die Uhr</option></select>
              ) : (
                <select value={effKind === "notional" ? "day" : tif} disabled={effKind === "notional"}
                        onChange={(e) => { setTif(e.target.value as "day" | "gtc"); reset(); }}>
                  <option value="day">day – bis Handelsschluss</option>
                  <option value="gtc">gtc – bis Widerruf (ganze Stück)</option>
                </select>
              )}
            </label>
          )}
        </div>
        {full && (
          <p className="field-help">
            Nur Market-Orders und Long-Positionen. Aktien: Betrags- und Bruchstück-Orders nur „day“. Krypto: immer „gtc“,
            Kauf zum Brief-, Verkauf zum Geldkurs, Gebühr je Ausführung. Verkäufe nur aus vorhandenem, freiem Bestand.
          </p>
        )}
        <button className="btn primary block" type="submit" disabled={busy !== null}>
          {busy === "preview" ? <Spinner /> : <Icon name="check" size={16} />} Prüfen &amp; Zusammenfassung
        </button>

        {preview && (
          <div className="stack" aria-live="polite">
            <dl className="kv">
              <dt>Instrument</dt><dd>{preview.symbol}{preview.asset_name ? ` – ${preview.asset_name}` : ""}</dd>
              <dt>Kurs (Schätzung)</dt><dd>{price(preview.est_price)} <span className="muted small">{preview.est_price_source ? `· ${preview.est_price_source}, ${dateTime(preview.est_price_at)}` : ""}</span></dd>
              <dt>Geschätzter Wert</dt><dd><strong>{money(preview.est_value, preview.currency)}</strong></dd>
              <dt>Gültigkeit</dt><dd>{preview.time_in_force}</dd>
            </dl>
            {(full ? preview.checks : failed).length > 0 && (
              <ul className="checks" aria-label="Prüfungen">
                {(full ? preview.checks : failed).map((c) => (
                  <li key={c.name}>
                    <span className={c.ok ? "ck-ok" : "ck-bad"} aria-label={c.ok ? "erfüllt" : "nicht erfüllt"}>{c.ok ? "✓" : "✗"}</span>
                    <span className="ck-name">{c.name}</span>
                    <span className="ck-detail">{c.detail}</span>
                  </li>
                ))}
              </ul>
            )}
            {!full && preview.allowed && <p className="field-help">Alle {preview.checks.length} Prüfungen erfüllt.</p>}
            {full && preview.warnings.map((w) => <p key={w} className="field-help">{w}</p>)}
            {preview.allowed ? (
              <button type="button" className={`btn block ${side === "buy" ? "buy" : "sell"}`} disabled={locked || busy !== null} onClick={submit}>
                {busy === "submit" ? <Spinner /> : null}
                {VERB[side]} {symbol} · {effKind === "notional" ? amountText : `${num(Number(amount.replace(",", ".")), 9)} Stk.`}
              </button>
            ) : (
              <Notice tone="error" title="Order gesperrt">Mindestens eine Prüfung ist nicht erfüllt (✗).</Notice>
            )}
          </div>
        )}
        {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      </form>
    </section>
  );
}
