// Тонкая обёртка над fetch: cookie сессии того же origin, ошибки — ApiError с текстом бэкенда.
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method: init?.method ?? "GET",
    headers: init?.body !== undefined ? { "content-type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  const data = (r.headers.get("content-type") ?? "").includes("json") ? await r.json() : null;
  if (!r.ok) {
    const d = data?.detail;
    throw new ApiError(r.status, typeof d === "string" ? d : Array.isArray(d) ? d[0]?.msg ?? "ошибка" : r.statusText);
  }
  return data as T;
}

export type Me = { id: number; username: string | null; role: string };
export type Config = { bot_username: string | null; password_enabled: boolean };
export type Status = {
  worker_alive: boolean;
  last_pass: { at: number; took?: number; sellers?: number; skipped?: number; paused?: boolean } | null;
  sellers: number;
  items: number;
};
export type Seller = {
  supplier_id: number; name: string | null; brand: string | null; b2b: boolean; enabled: boolean;
  loaded: boolean; last_check_at: string | null; items: number;
};
export type Item = {
  nm_id: number; name: string; brand: string | null; url: string; supplier_id: number; seller: string | null;
  b2b: boolean; price: number | null; price_stale: boolean; shelf_price: number | null;
  wallet_price: number | null; wallet_account: string | null; price_24h: number | null; is_new: boolean;
  best_price: number | null; best_how: string | null; target: number | null; below_target: boolean;
  stock: number; delivery_hours: number | null;
};
export type FeedEvent = {
  nm_id: number; name: string | null; supplier_id: number; seller: string | null; event: "new" | "change" | "gone";
  price: number | null; stock: number | null; old_price: number | null; old_stock: number | null; at: string;
  wallet_price: number | null; wallet_account: string | null;
};
export type Target = {
  nm_id: number; name: string | null; url: string; supplier_id: number | null; seller: string | null;
  target: number; best_price: number | null; best_how: string | null; gap: number | null; gap_pct: number | null;
  stock: number; below: boolean; below_at: string | null; created_at: string;
};
export type Order = {
  rid: string; nm_id: number | null; name: string | null; brand: string | null; price: number | null;
  dst_office_id: number | null; status_name: string | null; group_id: number | null;
  status_at: string | null; ordered_at: string | null; expiry_at: string | null;
};
export type Account = {
  id: number; label: string | null; phone_mask: string; name: string | null; phone: string | null;
  purchase_pct: number | null; kind: "business" | "retail";
  status: "active" | "relogin" | "off"; wallet_pct: number | null; limit_left: number | null;
  monthly_limit: number | null; max_order: number | null; max_rid: number | null;
  wallet_balance: number | null; bonus_balance: number | null; state_at: string | null; orders: Order[];
};
export type LoginRequest = {
  id: number; status: "new" | "running" | "wait_code" | "done" | "error"; kind: string;
  error: string | null; account_id: number | null; created_at: string;
} | null;

export const rub = (n: number | null | undefined) => (n == null ? "—" : `${n.toLocaleString("ru-RU")} ₽`);
export const time = (iso: string | number | null) => {
  if (iso == null) return "—";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString("ru-RU", today ? { hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};
