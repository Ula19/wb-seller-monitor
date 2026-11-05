import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, rub, type Account, type Item, type Seller } from "../api";
import { FeedList } from "./Events";

type Chip = "all" | "target" | "down" | "up" | "out";

// Цель ₽: пуш «ниже цели», когда лучшая цена (витрина/бизнес/с Кошельком) опустится до неё
function TargetCell({ it }: { it: Item }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [val, setVal] = useState("");
  const save = useMutation({
    mutationFn: (price: number | null) => price
      ? api(`/targets/${it.nm_id}`, { method: "PUT", body: { price } })
      : api(`/targets/${it.nm_id}`, { method: "DELETE" }),
    onSuccess: () => { setEdit(false); qc.invalidateQueries({ queryKey: ["items"] }); },
  });
  const base = it.best_price ?? it.shelf_price;
  if (edit) {
    return (
      <form className="tgt-edit" onSubmit={(e) => {
        e.preventDefault();
        const n = parseInt(val.replace(/\D/g, ""), 10);
        if (n > 0) save.mutate(n);
      }}>
        <input autoFocus inputMode="numeric" placeholder="₽" value={val} onChange={(e) => setVal(e.target.value)}
               onKeyDown={(e) => e.key === "Escape" && setEdit(false)} />
        <button className="btn sm" disabled={save.isPending}>OK</button>
      </form>
    );
  }
  if (it.target) {
    return (
      <span className="tgt">
        <button className="link" title="Изменить цель" onClick={() => { setVal(String(it.target)); setEdit(true); }}>{rub(it.target)}</button>
        <button className="x" title="Снять цель" onClick={() => save.mutate(null)}>✕</button>
      </span>
    );
  }
  return (
    <span className="tgt-quick">
      {base && base > 100 && <button className="mini" title={`Цель ${rub(base - 100)}`} onClick={() => save.mutate(base - 100)}>−100</button>}
      {base && base > 1000 && <button className="mini" title={`Цель ${rub(base - 1000)}`} onClick={() => save.mutate(base - 1000)}>−1000</button>}
      <button className="mini" title="Своя цель" onClick={() => { setVal(""); setEdit(true); }}>✎</button>
    </span>
  );
}

// Δ за сутки по витрине: товару моложе суток не считаем (он «новый»)
export const delta = (it: Item) =>
  !it.is_new && it.price_24h && it.shelf_price ? it.shelf_price - it.price_24h : 0;

export function Delta({ it }: { it: Item }) {
  if (it.is_new) return <span className="badge new">новый</span>;
  const d = delta(it);
  if (!d) return <span className="muted">—</span>;
  return <span className={`badge ${d < 0 ? "down" : "up"}`}>{d < 0 ? "▼" : "▲"} {rub(Math.abs(d))}</span>;
}

export default function Items() {
  const items = useQuery({ queryKey: ["items"], queryFn: () => api<Item[]>("/items"), refetchInterval: 60_000 });
  const sellers = useQuery({ queryKey: ["sellers"], queryFn: () => api<Seller[]>("/sellers") });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/accounts") });
  const [chip, setChip] = useState<Chip>("all");
  const [q, setQ] = useState("");
  const [seller, setSeller] = useState("");

  const all = items.data ?? [];
  const hasBiz = (accounts.data ?? []).some((a) => a.kind === "business");  // колонка — только при бизнес-аккаунте
  const count = {
    all: all.length,
    target: all.filter((i) => i.below_target).length,
    down: all.filter((i) => delta(i) < 0).length,
    up: all.filter((i) => delta(i) > 0).length,
    out: all.filter((i) => !i.stock).length,
  };
  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    return all.filter((i) =>
      (chip === "all" || (chip === "target" && i.below_target) || (chip === "down" && delta(i) < 0) || (chip === "up" && delta(i) > 0) || (chip === "out" && !i.stock))
      && (!seller || String(i.supplier_id) === seller)
      && (!s || `${i.name} ${i.brand ?? ""} ${i.nm_id}`.toLowerCase().includes(s)));
  }, [all, chip, q, seller]);

  const CHIPS: [Chip, string][] = [["all", "Все"], ["target", "🎯 Ниже цели"], ["down", "▼ Подешевели"], ["up", "▲ Подорожали"], ["out", "Нет в наличии"]];
  return (
    <div className="with-feed">
      <section>
        <h1>Товары</h1>
        <div className="tiles">
          <div className="tile"><b>{count.all}</b><span>в мониторинге</span></div>
          <div className="tile"><b className="ok-t">{count.target}</b><span>ниже цели</span></div>
          <div className="tile"><b className="down-t">{count.down}</b><span>▼ за 24 ч</span></div>
          <div className="tile"><b className="up-t">{count.up}</b><span>▲ за 24 ч</span></div>
          <div className="tile"><b>{count.out}</b><span>нет в наличии</span></div>
        </div>
        <div className="toolbar">
          <div className="chips">
            {CHIPS.map(([k, label]) => (
              <button key={k} className={`chip ${chip === k ? "on" : ""}`} onClick={() => setChip(k)}>
                {label} <span className="muted">{count[k]}</span>
              </button>
            ))}
          </div>
          <input className="search" placeholder="Поиск: название, бренд, артикул" value={q} onChange={(e) => setQ(e.target.value)} />
          <select value={seller} onChange={(e) => setSeller(e.target.value)}>
            <option value="">Все продавцы</option>
            {(sellers.data ?? []).map((s) => <option key={s.supplier_id} value={s.supplier_id}>{s.name ?? s.supplier_id}</option>)}
          </select>
        </div>
        {items.isPending ? <p className="muted">Загрузка…</p> : !all.length ? (
          <div className="card empty">Товаров пока нет. Добавьте продавца на странице «Продавцы» — первая загрузка займёт минуту.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Товар</th><th>Продавец</th><th className="num">Витрина</th><th className="num">С Кошельком</th>
                  {hasBiz && <th className="num">Бизнес</th>}<th className="num">Δ 24 ч</th><th className="num">Остаток</th><th>Цель</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((i) => (
                  <tr key={`${i.supplier_id}-${i.nm_id}`} className={i.below_target ? "hit" : i.stock ? "" : "dim"}
                      title={i.below_target ? `Ниже цели: ${rub(i.best_price)} · ${i.best_how}` : undefined}>
                    <td className="name">
                      <a href={i.url} target="_blank" rel="noreferrer">{i.name}</a>
                      <div className="muted small">{i.brand ? `${i.brand} · ` : ""}арт. {i.nm_id}</div>
                    </td>
                    <td>{i.seller ?? i.supplier_id}</td>
                    <td className="num">{rub(i.shelf_price)}</td>
                    <td className="num">
                      {i.wallet_price ? <>{rub(i.wallet_price)}<div className="muted small">{i.wallet_account}</div></> : <span className="muted">—</span>}
                    </td>
                    {hasBiz && <td className="num">{i.b2b ? (i.price_stale ? <span className="muted small">не подтверждена</span> : rub(i.price)) : <span className="muted">—</span>}</td>}
                    <td className="num"><Delta it={i} /></td>
                    <td className="num">{i.stock || <span className="pill warn">нет</span>}</td>
                    <td className="target-cell"><TargetCell it={i} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!shown.length && <p className="muted pad">Ничего не нашлось по фильтрам.</p>}
          </div>
        )}
      </section>
      <aside className="feed-side">
        <h2>Лента</h2>
        <FeedList limit={20} />
      </aside>
    </div>
  );
}
