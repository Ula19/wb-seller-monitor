// Тонкая обёртка над fetch: cookie сессии того же origin, ошибки — ApiError с текстом бэкенда.
export class ApiError extends Error {
  // detail — тело ошибки как есть (объект для 409 already_connected и т.п.)
  constructor(public status: number, message: string, public detail: unknown = null) {
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
    throw new ApiError(r.status, typeof d === "string" ? d : Array.isArray(d) ? d[0]?.msg ?? "ошибка" : r.statusText, d);
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
  status_at: string | null; ordered_at: string | null; expiry_at: string | null; delivery_at: string | null;
  paid: boolean | null; office_address: string | null; office_hours: string | null; hidden: boolean;
};
export type Account = {
  id: number; label: string | null; phone_mask: string; name: string | null; phone: string | null;
  purchase_pct: number | null; kind: "business" | "retail";
  status: "active" | "relogin" | "off"; wallet_pct: number | null; limit_left: number | null;
  monthly_limit: number | null; max_order: number | null; max_rid: number | null;
  funds: number | null; vb_points: number | null; state_at: string | null; orders: Order[];
  wallet_blocked: boolean | null; wallet_arrested: number | null;  // кошелёк заблокирован, ₽ под арестом
  proxy_fails: number; proxy_fail_at: string | null; proxy_down: boolean; polling: boolean;
  visit_error: string | null; site_lost: boolean;  // почему визит в браузере не удался; сессия сайта истекла
  title: string; proxy: { id: number; label: string; server: string; exit_ip: string | null } | null;
};
// страница аккаунта: + код получения на сегодня и счётчики вкладок
export type AccountFull = Account & {
  browser_sync: boolean; pickup_code: string | null; counts: { purchases: number; balance: number; vb: number };
};
export type Purchase = {
  rid: string; nm_id: number | null; name: string | null; brand: string | null; price: number | null; dt: string | null;
  office_id: number | null; office_address: string | null; office_hours: string | null;
};
export type BalanceOp = {
  tx_id: string; name: string | null; op_type: number | null; amount: number | null; balance: number | null;
  dt: string | null; note: string | null; cert_code: string | null;  // код сертификата, если операция — его активация
  source_type: string | null;  // sourceType WB: giftcards — пополнение сертификатом (в т.ч. активированным на самом WB)
};
export type Cert = {
  id: number; code: string; account_id: number | null; nominal_entered: number; nominal: number | null;
  status: "pending" | "sending" | "ok" | "error" | "unknown"; error_text: string | null; created_at: string;
  done_at: string | null; account_title: string | null; funds: number | null;
};
export type VbOp = {
  tx_id: string; title: string | null; description: string | null; dt: string | null; operation: number | null;
  amount: number | null;
};
export type Proxy = {
  id: number; label: string; server: string; exit_ip: string | null; ok: boolean | null; error: string | null;
  checking: boolean; checked_at: string | null; created_at: string; used_by: string[];
};
export type LoginRequest = {
  id: number; status: "new" | "running" | "wait_code" | "done" | "error" | "cancelled"; kind: string;
  error: string | null; account_id: number | null; created_at: string;
  sent_via: "PUSH" | "SMS" | null; resend_after: string | null; resend: "push" | "sms" | null;
} | null;

export const rub = (n: number | null | undefined) => (n == null ? "—" : `${n.toLocaleString("ru-RU")} ₽`);
export const time = (iso: string | number | null) => {
  if (iso == null) return "—";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString("ru-RU", today ? { hour: "2-digit", minute: "2-digit" }
    : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};
