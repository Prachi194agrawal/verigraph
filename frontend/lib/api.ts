// Thin client for the FastAPI backend. NEXT_PUBLIC_API_URL is baked in at build time.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

let authorization: string | null = null;

export function setCredentials(username: string | null, password = "") {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  authorization = username === null ? null : `Basic ${btoa(Array.from(bytes, b => String.fromCharCode(b)).join(""))}`;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(authorization ? { Authorization: authorization } : {}), ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      // not JSON: keep the status text
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "POST", body: JSON.stringify(body) });
}

export function fmt(value: number | null | undefined, digits = 3): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  return value.toFixed(digits);
}

export function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return `${(value * 100).toFixed(1)}%`;
}
