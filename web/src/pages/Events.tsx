import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, rub, time, type FeedEvent } from "../api";

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
        <span className="muted small">{time(e.at)}</span>
      </div>
      <a href={`https://www.wildberries.ru/catalog/${e.nm_id}/detail.aspx`} target="_blank" rel="noreferrer">
        {e.name ?? `арт. ${e.nm_id}`}
      </a>
      <div className="small">
        {k === "down" || k === "up" ? <><s className="muted">{rub(e.old_price)}</s> → <b>{rub(e.price)}</b></>
          : k === "other" ? <>остаток {e.old_stock} → {e.stock}</> : rub(e.price)}
      </div>
    </li>
  );
}

export function FeedList({ limit, filter }: { limit: number; filter?: Kind | "all" }) {
  const feed = useQuery({ queryKey: ["feed", limit], queryFn: () => api<FeedEvent[]>(`/feed?limit=${limit}`), refetchInterval: 60_000 });
  if (feed.isPending) return <p className="muted">Загрузка…</p>;
  const rows = (feed.data ?? []).filter((e) => !filter || filter === "all" || kindOf(e) === filter);
  if (!rows.length) return <p className="muted small">Событий пока нет — они появятся, когда у товаров изменится цена или наличие.</p>;
  return <ul className="feed">{rows.map((e, i) => <Line key={`${e.nm_id}-${e.at}-${i}`} e={e} />)}</ul>;
}

export default function Events() {
  const [f, setF] = useState<Kind | "all">("all");
  const CH: [Kind | "all", string][] = [["all", "Все"], ["down", "▼ Подешевели"], ["up", "▲ Подорожали"],
    ["in", "Появились"], ["out", "Закончились"], ["new", "Новинки"], ["gone", "Пропали"]];
  return (
    <section>
      <h1>События</h1>
      <div className="chips">
        {CH.map(([k, l]) => <button key={k} className={`chip ${f === k ? "on" : ""}`} onClick={() => setF(k)}>{l}</button>)}
      </div>
      <div className="card"><FeedList limit={300} filter={f} /></div>
    </section>
  );
}
