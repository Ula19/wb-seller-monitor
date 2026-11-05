import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, rub, time, type FeedEvent, type Seller } from "../api";

type Kind = "down" | "up" | "in" | "out" | "new" | "gone" | "other";

export function kindOf(e: FeedEvent): Kind {
  if (e.event === "new") return "new";
  if (e.event === "gone") return "gone";
  const wasIn = (e.old_stock ?? 0) > 0, isIn = (e.stock ?? 0) > 0;
  if (wasIn !== isIn) return isIn ? "in" : "out";
  if (e.old_price && e.price && e.price !== e.old_price) return e.price < e.old_price ? "down" : "up";
  return "other";  // сдвинулся только остаток
}

const LABEL: Record<Kind, string> = {
  down: "▼ подешевел", up: "▲ подорожал", in: "появился в наличии", out: "закончился",
  new: "новинка", gone: "пропал из продажи", other: "остаток",
};

function Line({ e }: { e: FeedEvent }) {
  const k = kindOf(e);
  return (
    <li className="ev">
      <div className="ev-head">
        <span className={`badge ${k}`}>{LABEL[k]}</span>
        <span className="muted small">{e.seller ?? e.supplier_id} · {time(e.at)}</span>
      </div>
      <a href={`https://www.wildberries.ru/catalog/${e.nm_id}/detail.aspx`} target="_blank" rel="noreferrer">
        {e.name ?? `арт. ${e.nm_id}`}
      </a>
      <div className="small">
        {k === "down" || k === "up" ? <><s className="muted">{rub(e.old_price)}</s> → <b>{rub(e.price)}</b></>
          : k === "other" ? <>остаток {e.old_stock} → {e.stock}</> : rub(e.price)}
        {e.wallet_price != null && <span className="muted"> · с Кошельком <b className="wallet-t">{rub(e.wallet_price)}</b> ({e.wallet_account})</span>}
      </div>
    </li>
  );
}

export default function Events() {
  const feed = useQuery({ queryKey: ["feed"], queryFn: () => api<FeedEvent[]>("/feed?limit=500"), refetchInterval: 60_000 });
  const sellers = useQuery({ queryKey: ["sellers"], queryFn: () => api<Seller[]>("/sellers") });
  const [f, setF] = useState<Kind | "all">("all");
  const [q, setQ] = useState("");
  const [seller, setSeller] = useState("");
  const s = q.trim().toLowerCase();
  const rows = (feed.data ?? []).filter((e) => (f === "all" || kindOf(e) === f)
    && (!seller || String(e.supplier_id) === seller)
    && (!s || `${e.name ?? ""} ${e.nm_id}`.toLowerCase().includes(s)));
  const CH: [Kind | "all", string][] = [["all", "Все"], ["down", "▼ Подешевели"], ["up", "▲ Подорожали"],
    ["in", "Появились"], ["out", "Закончились"], ["new", "Новинки"], ["gone", "Пропали"]];
  return (
    <section>
      <h1>События</h1>
      <p className="lead">Журнал изменений: строка на каждое изменение цены или наличия. Текущие цены — в «Товарах».</p>
      <div className="toolbar">
        <div className="chips">
          {CH.map(([k, l]) => <button key={k} className={`chip ${f === k ? "on" : ""}`} onClick={() => setF(k)}>{l}</button>)}
        </div>
        <input className="search" placeholder="Поиск: название, артикул" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={seller} onChange={(e) => setSeller(e.target.value)}>
          <option value="">Все продавцы</option>
          {(sellers.data ?? []).map((x) => <option key={x.supplier_id} value={x.supplier_id}>{x.name ?? x.supplier_id}</option>)}
        </select>
      </div>
      <div className="card">
        {feed.isPending ? <p className="muted">Загрузка…</p> : !rows.length
          ? <p className="muted small">{feed.data?.length ? "Ничего не нашлось по фильтрам." : "Событий пока нет — они появятся, когда у товаров изменится цена или наличие."}</p>
          : <ul className="feed">{rows.map((e, i) => <Line key={`${e.nm_id}-${e.at}-${i}`} e={e} />)}</ul>}
      </div>
    </section>
  );
}
