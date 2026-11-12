// Общее для «Аккаунтов» (список) и страницы аккаунта: подписи, лимит, имя, картинки товаров.
import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, MSK, mskDay, rub, type Account, type Order } from "./api";
import { canTopUp, overConfirm } from "./certs";

export const STATUS: Record<string, string> = { active: "активен", relogin: "нужен вход", off: "выключен" };
export const GROUP: Record<number, string> = { 1: "оформлен", 2: "сборка", 3: "в пути", 5: "в ПВЗ" };

export const visible = (orders: Order[]) => orders.filter((o) => !o.hidden);
export const transit = (a: Account) => visible(a.orders).filter((o) => o.group_id === 3).length;
export const inPvz = (a: Account) => visible(a.orders).filter((o) => o.group_id === 5).length;
/** «Не оплачено»: сумма неоплаченных активных заказов (paid === false; «не знаем» не считаем), без скрытых. */
export const unpaid = (a: Account) => visible(a.orders).filter((o) => o.paid === false).reduce((s, o) => s + (o.price ?? 0), 0);
export const ddmm = (iso: string) => new Date(iso).toLocaleDateString("ru-RU", { timeZone: MSK, day: "2-digit", month: "2-digit" });
/** Дата и время по МСК: 11.11.26, 14:05. */
export const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("ru-RU",
  { timeZone: MSK, day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");
export const points = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("ru-RU"));

// сброс месячного лимита — 1-е число следующего месяца
export const resetDate = () => `01.${String((Number(mskDay(Date.now()).slice(5, 7)) % 12) + 1).padStart(2, "0")}`;

export const topUp = (a: Account) => canTopUp(a.limit_left, a.funds ?? 0);
/** Перед пополнением: кто сверх лимита — confirm со списком; никого — сразу true. rows — сколько ₽ идёт на аккаунт. */
export const confirmTopUp = (rows: { a: Account; n: number }[]) => {
  const t = overConfirm(rows.map(({ a, n }) => ({ title: a.title, n, can: topUp(a) })));
  return !t || confirm(t);
};

/** Лимит при пополнении одной строкой; n — сколько ₽ сертификатами идёт на аккаунт: больше «можно» — красным. */
export function LimitNote({ a, n }: { a: Account; n: number }) {
  const can = topUp(a);
  return (
    <div className="lim-note small">
      {can == null ? <div className="muted">Лимит неизвестен</div> : <>
        <div className="muted">Лимит {rub(a.limit_left)} · можно пополнить на {rub(can)}</div>
        {n > can && <div className="red-box">Сверх лимита на {rub(n - can)}</div>}
      </>}
    </div>
  );
}

/** Окно поверх страницы; Esc закрывает. backClose — и клик по фону (где нечего потерять). */
export function Modal({ label, onClose, wide = false, backClose = false, children }: {
  label: string; onClose: () => void; wide?: boolean; backClose?: boolean; children: ReactNode;
}) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="modal-back" onClick={(e) => backClose && e.target === e.currentTarget && onClose()}>
      <div className={`modal card${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={label}>
        <button className="modal-x link" aria-label="Закрыть" onClick={onClose}>✕</button>
        {children}
      </div>
    </div>
  );
}

// ---------- период «С / По» + пресеты (дни — по МСК, как режет сервер) ----------
const daysAgo = (n: number) => mskDay(Date.now() - n * 86400_000);

/** days — пресет по умолчанию (null — всё время). */
export function usePeriod(days: number | null) {
  const [from, setFrom] = useState(() => (days == null ? "" : daysAgo(days - 1)));
  const [to, setTo] = useState(() => (days == null ? "" : daysAgo(0)));
  const preset = (d: number | null) => { setFrom(d == null ? "" : daysAgo(d - 1)); setTo(d == null ? "" : daysAgo(0)); };
  const active = (d: number | null) => (d == null ? !from && !to : to === daysAgo(0) && from === daysAgo(d - 1));
  const has = (dt: string | null) => (!from && !to) || (!!dt && (!from || mskDay(dt) >= from) && (!to || mskDay(dt) <= to));
  return { from, to, setFrom, setTo, preset, active, has };
}

export function Period({ p, presets = [null, 7, 30] }: { p: ReturnType<typeof usePeriod>; presets?: (number | null)[] }) {
  return <>
    <label className="small">С <input type="date" value={p.from} max={p.to || undefined} onChange={(e) => p.setFrom(e.target.value)} /></label>
    <label className="small">По <input type="date" value={p.to} min={p.from || undefined} onChange={(e) => p.setTo(e.target.value)} /></label>
    <div className="seg">
      {presets.map((d) => <button key={String(d)} className={p.active(d) ? "on" : ""} onClick={() => p.preset(d)}>
        {d == null ? "Всё время" : d === 1 ? "1 день" : `${d} дней`}</button>)}
    </div>
  </>;
}

/** Код сертификата: клик — в буфер обмена, на 1,5 с «скопировано». */
export function CertCode({ code }: { code: string }) {
  const [done, setDone] = useState("");
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setDone("скопировано"); } catch { setDone("не скопировалось"); }
    setTimeout(() => setDone(""), 1500);
  };
  return <button className="cert-code" title="Скопировать код сертификата" onClick={copy}>
    <code>{code}</code>{done && <span className={`small ${done === "скопировано" ? "ok-t" : "bad-t"}`}> {done}</span>}</button>;
}

export function StatusDot({ a }: { a: Account }) {
  return <i className={`dot ${a.status === "active" ? "ok" : a.status === "relogin" ? "bad" : "off"}`} title={STATUS[a.status]} />;
}

/** «Кошелёк заблокирован» — WB не даёт платить Кошельком (сертификаты на баланс при этом идут). */
export const Blocked = ({ a }: { a: Account }) => a.wallet_blocked
  ? <span className="pill bad" title="WB наложил ограничение на Кошелёк — оплата им недоступна">Кошелёк заблокирован</span> : null;

/** Полоса лимита скидки + остаток; оранжевым при < 10 %, красным при 0. */
export function Limit({ a, short = false }: { a: Account; short?: boolean }) {
  if (a.limit_left == null) return <div className="muted">—</div>;
  const low = a.limit_left === 0 ? "bad-t" : a.monthly_limit && a.limit_left < a.monthly_limit * 0.1 ? "up-t" : "";
  const pct = a.monthly_limit ? Math.max(0, Math.min(100, (a.limit_left / a.monthly_limit) * 100)) : null;
  return (
    <div className="limit">
      {pct != null && <div className="bar"><i className={low} style={{ width: `${pct}%` }} /></div>}
      <span className={low}>{rub(a.limit_left)}</span>
      {!short && a.monthly_limit ? <span className="muted"> из {rub(a.monthly_limit)} · сброс {resetDate()}</span> : null}
      {!short && a.limit_left === 0 && <div className="bad-t small">лимит исчерпан — Кошелёк без скидки</div>}
      {!short && low === "up-t" && <div className="up-t small">лимит почти исчерпан</div>}
    </div>
  );
}

/** Имя аккаунта со своим ✎ (главнее имени из WB). */
export function Title({ a }: { a: Account }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api(`/accounts/${a.id}`, { method: "PATCH", body: { label: edit } }),
    onSuccess: () => { setEdit(null); qc.invalidateQueries({ queryKey: ["accounts"] }); },
  });
  if (edit !== null) return (
    <form className="tgt-edit" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <input autoFocus maxLength={64} placeholder={a.name ?? "Имя"} value={edit} onChange={(e) => setEdit(e.target.value)} />
      <button className="btn sm" disabled={save.isPending}>OK</button>
      <button type="button" className="link" onClick={() => setEdit(null)}>✕</button>
    </form>
  );
  return (
    <span>
      <b>{a.title}</b>{" "}
      <button className="mini" title="Своё имя аккаунта" onClick={() => setEdit(a.label ?? "")}>✎</button>
      {a.name && a.name !== a.label && <span className="muted small"> {a.label ? `в WB: ${a.name}` : "(имя из WB)"}</span>}
    </span>
  );
}

/** Картинки товаров: карту basket-хостов кладёт worker раз в сутки, URL собираем тут. */
export function useImg() {
  const hosts = useQuery({ queryKey: ["img-hosts"], queryFn: () => api<[number, number, string][]>("/img-hosts"),
    staleTime: Infinity });
  return (nm: number | null) => {
    if (!nm) return null;
    const vol = Math.floor(nm / 100000);
    const host = hosts.data?.find(([a, b]) => a <= vol && vol <= b)?.[2];
    return host ? `https://${host}/vol${vol}/part${Math.floor(nm / 1000)}/${nm}/images/c246x328/1.webp` : null;
  };
}

export function Thumb({ src }: { src: string | null }) {
  const [bad, setBad] = useState(false);
  return src && !bad ? <img className="thumb" src={src} alt="" loading="lazy" onError={() => setBad(true)} />
    : <span className="thumb none">📦</span>;
}

// ---------- заказы: группы по ПВЗ, строки по товару ----------
export type Line = { key: string; orders: Order[]; first: Order };  // одинаковый товар в одном статусе — одна строка «× N»
export type Pvz = { id: number; address: string; hours: string | null; orders: Order[] };

export function byPvz(orders: Order[]): Pvz[] {
  const m = new Map<number, Pvz>();
  for (const o of orders) {
    const id = o.dst_office_id ?? 0;
    const g = m.get(id) ?? { id, address: o.office_address ?? (id ? `ПВЗ №${id}` : "ПВЗ не указан"), hours: o.office_hours, orders: [] };
    g.orders.push(o);
    m.set(id, g);
  }
  // сначала те, где есть что забирать
  return [...m.values()].sort((a, b) => Number(b.orders.some((o) => o.group_id === 5)) - Number(a.orders.some((o) => o.group_id === 5)));
}

export function lines(orders: Order[]): Line[] {
  const m = new Map<string, Line>();
  for (const o of orders) {
    const key = [o.nm_id ?? o.rid, o.group_id, o.paid, o.price, o.hidden].join("|");
    const l = m.get(key) ?? { key, orders: [], first: o };
    l.orders.push(o);
    m.set(key, l);
  }
  return [...m.values()];
}

/** Текст «Готовы забрать» для буфера обмена — только то, что ждёт в ПВЗ, без скрытых; сумма — за все штуки. */
export function pickupText(g: Pvz, a: Account, code: string): string {
  const phone = (a.phone ?? "").replace(/\D/g, "").slice(-10);
  const items = new Map<string, { name: string; n: number; sum: number }>();
  for (const o of g.orders.filter((o) => o.group_id === 5 && !o.hidden)) {
    const k = String(o.nm_id ?? o.rid);
    const it = items.get(k) ?? { name: o.name ?? `арт. ${o.nm_id}`, n: 0, sum: 0 };
    it.n += 1;
    it.sum += o.price ?? 0;
    items.set(k, it);
  }
  const head = [g.address, `${phone ? "7" + phone : ""} ${a.title}`.trim(), `Код : ${code}`];
  return [head.join("\n"), ...[...items.values()].map((it) => `${it.name} - ${it.n} шт\n${it.sum} руб`)].join("\n\n");
}
