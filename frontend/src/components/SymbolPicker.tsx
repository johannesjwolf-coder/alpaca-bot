import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { usePoll } from "../hooks";
import type { Mode, SymbolSuggestion, SymbolsResponse } from "../types";
import { Icon } from "./ui";

const SYMBOL_RE = /^(?:[A-Z]{1,5}(?:\.[A-Z]{1,2})?|[A-Z0-9]{2,10}\/USD)$/;

export const kindLabel = (symbol: string) => (symbol.includes("/") ? "Krypto" : "Aktie/ETF");

/** Vorschläge ohne Suchbegriff (Bot-Symbol, Bestände, zuletzt gehandelt, beliebt). */
export function useSymbolSuggestions(mode: Mode) {
  return usePoll(() => api.get<SymbolsResponse>("/symbols"), 60_000, [mode]);
}

export function QuickSymbols({ items, value, onSelect, max = 6 }: {
  items: SymbolSuggestion[]; value: string; onSelect: (s: string) => void; max?: number;
}) {
  if (!items.length) return null;
  return (
    <div className="quick-symbols" aria-label="Vorgeschlagene Symbole">
      {items.slice(0, max).map((i) => (
        <button key={i.symbol} type="button" aria-pressed={i.symbol === value} title={`${i.name || i.symbol} · ${i.group}`}
                onClick={() => onSelect(i.symbol)}>
          {i.symbol}
        </button>
      ))}
    </div>
  );
}

export function SymbolPicker({ value, onChange, mode, label = "Symbol wählen", compact }: {
  value: string; onChange: (symbol: string) => void; mode: Mode; label?: string; compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [items, setItems] = useState<SymbolSuggestion[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    const t = window.setTimeout(async () => {
      try {
        const res = await api.get<SymbolsResponse>(`/symbols?q=${encodeURIComponent(q)}`);
        if (!cancelled) {
          setItems(res.items);
          setNote(res.note);
          setActive(0);
        }
      } catch {
        if (!cancelled) setNote("Vorschläge konnten nicht geladen werden.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, q ? 220 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [q, open, mode]);

  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const onDoc = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const typed = q.trim().toUpperCase();
  const typedValid = SYMBOL_RE.test(typed) && !items.some((i) => i.symbol === typed);
  const groups = useMemo(() => {
    const out: { group: string; items: SymbolSuggestion[] }[] = [];
    for (const it of items) {
      const g = out.find((x) => x.group === it.group);
      if (g) g.items.push(it);
      else out.push({ group: it.group, items: [it] });
    }
    return out;
  }, [items]);
  const flat = items;

  function choose(symbol: string) {
    onChange(symbol);
    setOpen(false);
    setQ("");
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (typedValid && (!flat.length || !flat[active]?.symbol.startsWith(typed))) choose(typed);
      else if (flat[active]) choose(flat[active].symbol);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="symbol-picker" ref={root}>
      <button type="button" className="symbol-btn" aria-haspopup="listbox" aria-expanded={open} aria-label={`${label}: ${value}`}
              onClick={() => setOpen((o) => !o)}>
        {value}
        {!compact && <span className="kind">{kindLabel(value)}</span>}
        <Icon name="chevron" size={16} />
      </button>
      {open && (
        <div className="symbol-pop" role="dialog" aria-label={label}>
          <div className="search">
            <label className="field">
              <span className="visually-hidden">Symbol oder Name suchen</span>
              <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
                     placeholder="Symbol oder Name, z. B. BTC/USD, SPY, Apple" autoComplete="off" role="combobox"
                     aria-expanded="true" aria-controls="symbol-list" />
            </label>
          </div>
          <div id="symbol-list" role="listbox" aria-label="Vorschläge">
            {typedValid && (
              <button className="symbol-opt" role="option" aria-selected={false} onClick={() => choose(typed)}>
                <span className="sym">{typed}</span>
                <span className="name">übernehmen ({kindLabel(typed)})</span>
              </button>
            )}
            {groups.map((g) => (
              <div key={g.group}>
                <div className="symbol-group">{g.group}</div>
                {g.items.map((it) => {
                  const idx = flat.indexOf(it);
                  return (
                    <button key={it.symbol} className="symbol-opt" role="option" aria-selected={idx === active}
                            onMouseEnter={() => setActive(idx)} onClick={() => choose(it.symbol)}>
                      <span className="sym" style={{ minWidth: 76 }}>{it.symbol}</span>
                      <span className="name">{it.name || kindLabel(it.symbol)}</span>
                      <span className="spacer" />
                      <span className="tag">{it.asset_class === "crypto" ? "Krypto" : "Aktie/ETF"}</span>
                    </button>
                  );
                })}
              </div>
            ))}
            {!loading && !items.length && !typedValid && (
              <div className="empty-state sm" style={{ padding: 20 }}>Keine Treffer. US-Aktien wie <code>AAPL</code> oder Krypto wie <code>BTC/USD</code>.</div>
            )}
            {loading && <div className="symbol-group">Lade Vorschläge…</div>}
            {note && <div className="field-help" style={{ padding: "8px 12px" }}>{note}</div>}
          </div>
        </div>
      )}
    </div>
  );
}
