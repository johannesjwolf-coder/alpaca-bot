// Alle Aufrufe gehen an dieselbe Origin (/api). Zugangsdaten gibt es im Frontend nicht.

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        // CSRF-Schutz: Das Backend verlangt diesen Header bei ändernden Aufrufen.
        "X-Requested-With": "alpaca-bot-ui",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Backend nicht erreichbar. Läuft der Server?");
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
};

export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object") {
      const o = d as { message?: string; hint?: string; msg?: string };
      if (Array.isArray(d)) {
        return d.map((x: { msg?: string; loc?: unknown[] }) => `${(x.loc ?? []).slice(-1)}: ${x.msg}`).join("; ");
      }
      return [o.message ?? o.msg, o.hint].filter(Boolean).join(" – ") || `HTTP ${err.status}`;
    }
    return `HTTP ${err.status}`;
  }
  return err instanceof Error ? err.message : String(err);
}

export function newKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}
