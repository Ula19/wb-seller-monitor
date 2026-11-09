// Общее для «Аккаунтов» (список) и страницы аккаунта: подписи, лимит, имя, картинки товаров.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, rub, type Account, type Order } from "./api";

export const STATUS: Record<string, string> = { active: "активен", relogin: "нужен вход", off: "выключен" };
export const GROUP: Record<number, string> = { 1: "оформлен", 2: "сборка", 3: "в пути", 5: "в ПВЗ" };

export const visible = (orders: Order[]) => orders.filter((o) => !o.hidden);
export const transit = (a: Account) => visible(a.orders).filter((o) => o.group_id === 3).length;
export const inPvz = (a: Account) => visible(a.orders).filter((o) => o.group_id === 5).length;
export const ddmm = (iso: string) => new Date(iso).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
export const points = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("ru-RU"));

// сброс месячного лимита — 1-е число следующего месяца
export const resetDate = () => {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth() + 1, 1).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
};

export function StatusDot({ a }: { a: Account }) {
  return <i className={`dot ${a.status === "active" ? "ok" : a.status === "relogin" ? "bad" : "off"}`} title={STATUS[a.status]} />;
}

/** Полоса лимита скидки + остаток; оранжевым при < 10 %, красным при 0. */
export function Limit({ a, short = false }: { a: Account; short?: boolean }) {
  if (a.limit_left == null) return <span className="muted">—</span>;
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
