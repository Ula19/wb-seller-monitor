import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, rub, time, type Target } from "../api";
import { useSort } from "../sort";
import TargetCell from "../TargetCell";

type Chip = "all" | "below" | "above";

export default function Targets() {
  const targets = useQuery({ queryKey: ["targets"], queryFn: () => api<Target[]>("/targets"), refetchInterval: 60_000 });
  const [chip, setChip] = useState<Chip>("all");
  const all = targets.data ?? [];
  const shown = all.filter((t) => chip === "all" || (chip === "below") === t.below);
  const { sorted, th } = useSort(shown, {
    target: (t) => t.target, best: (t) => t.best_price, gap: (t) => t.gap_pct,
    stock: (t) => t.stock, at: (t) => (t.below_at ? -new Date(t.below_at).getTime() : null),
  }, "targets");
  const below = all.filter((t) => t.below).length;
  return (
    <section>
      <h1>Цели</h1>
      <p className="lead">
        Когда лучшая цена товара (витрина, бизнес или с Кошельком) опустится до цели и он в наличии — придёт пуш в Telegram.
        Ставить цель можно и в «Товарах».
      </p>
      <div className="chips">
        {([["all", `Все ${all.length}`], ["below", `🎯 Ниже цели ${below}`], ["above", `Выше цели ${all.length - below}`]] as [Chip, string][])
          .map(([k, l]) => <button key={k} className={`chip ${chip === k ? "on" : ""}`} onClick={() => setChip(k)}>{l}</button>)}
      </div>
      {targets.isPending ? <p className="muted">Загрузка…</p> : !all.length ? (
        <div className="card empty">Целей пока нет. В «Товарах» нажмите −100 / −1000 или ✎ в колонке «Цель».</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Товар</th>{th("target", "Цель", "")}{th("best", "Лучшая цена сейчас")}
                {th("gap", "До цели")}{th("stock", "Остаток")}<th>Статус</th>{th("at", "Была ниже", "")}
              </tr>
            </thead>
            <tbody>
              {sorted.map((t) => (
                <tr key={t.nm_id} className={t.below ? "hit" : t.stock ? "" : "dim"}>
                  <td className="name">
                    <a href={t.url} target="_blank" rel="noreferrer">{t.name ?? `арт. ${t.nm_id}`}</a>
                    <div className="muted small">{t.seller ? `${t.seller} · ` : ""}арт. {t.nm_id}</div>
                  </td>
                  <td className="target-cell"><TargetCell nm_id={t.nm_id} target={t.target} base={t.best_price} /></td>
                  <td className="num">{rub(t.best_price)}<div className="muted small">{t.best_how}</div></td>
                  <td className="num">
                    {t.gap == null ? "—" : t.gap <= 0 ? <span className="ok-t">ниже на {rub(-t.gap)}</span> : rub(t.gap)}
                    {t.gap_pct != null && <div className="muted small">{t.gap_pct > 0 ? "+" : ""}{t.gap_pct}%</div>}
                  </td>
                  <td className="num">{t.stock || <span className="pill warn">нет</span>}</td>
                  <td>{t.below ? <span className="pill ok">ниже цели</span> : <span className="pill">выше</span>}</td>
                  <td className="muted small">{t.below_at ? time(t.below_at) : "ещё не была"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
