import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, time, type Account, type Seller } from "../api";

// ID или ссылка вида wildberries.ru/seller/123 → 123
export const parseSellerId = (s: string) => {
  const m = s.trim().match(/seller\/(\d+)/) ?? s.trim().match(/^(\d+)$/);
  return m ? Number(m[1]) : null;
};

export default function Sellers() {
  const qc = useQueryClient();
  const sellers = useQuery({ queryKey: ["sellers"], queryFn: () => api<Seller[]>("/sellers"), refetchInterval: 30_000 });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/accounts") });
  const [input, setInput] = useState("");
  const [b2b, setB2b] = useState(false);
  const [error, setError] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["sellers"] }).then(() => qc.invalidateQueries({ queryKey: ["items"] }));

  const add = useMutation({
    mutationFn: (id: number) => api("/sellers", { method: "POST", body: { supplier_id: id, b2b } }),
    onSuccess: () => { setInput(""); setError(""); refresh(); },
    onError: (e) => setError(e instanceof ApiError ? e.message : "ошибка"),
  });
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<Seller> }) => api(`/sellers/${id}`, { method: "PATCH", body }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api(`/sellers/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const id = parseSellerId(input);
    if (!id) return setError("Нужен ID продавца или ссылка вида wildberries.ru/seller/123");
    add.mutate(id);
  };
  const hasBiz = (accounts.data ?? []).some((a) => a.kind === "business" && a.status === "active");

  return (
    <section>
      <h1>Продавцы</h1>
      <form className="card add" onSubmit={submit}>
        <input placeholder="ID продавца или ссылка на магазин WB" value={input} onChange={(e) => setInput(e.target.value)} />
        <select value={b2b ? "1" : "0"} onChange={(e) => setB2b(e.target.value === "1")}>
          <option value="0">Розничная цена</option>
          <option value="1">Бизнес-цена</option>
        </select>
        <button className="btn" disabled={add.isPending || !input.trim()}>+ Добавить</button>
        {error && <p className="err full">{error}</p>}
        {b2b && !hasBiz && <p className="muted small full">Бизнес-цены идут под вашим бизнес-аккаунтом WB — подключите его на странице «Аккаунты».</p>}
      </form>
      {sellers.isPending ? <p className="muted">Загрузка…</p> : !sellers.data?.length ? (
        <div className="card empty">Пока ни одного продавца. Добавьте первого — имя и товары подтянутся в течение минуты.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Продавец</th><th className="num">Товаров</th><th>Цена</th><th>Проверен</th><th>Мониторинг</th><th /></tr></thead>
            <tbody>
              {sellers.data.map((s) => (
                <tr key={s.supplier_id} className={s.enabled ? "" : "dim"}>
                  <td className="name">
                    <a href={`https://www.wildberries.ru/seller/${s.supplier_id}`} target="_blank" rel="noreferrer">
                      {s.name ?? `Продавец ${s.supplier_id}`}
                    </a>
                    <div className="muted small">ID {s.supplier_id}{!s.loaded && " · загружается…"}</div>
                  </td>
                  <td className="num">{s.items}</td>
                  <td>
                    <div className="seg">
                      <button className={!s.b2b ? "on" : ""} onClick={() => patch.mutate({ id: s.supplier_id, body: { b2b: false } })}>Розница</button>
                      <button className={s.b2b ? "on" : ""} onClick={() => patch.mutate({ id: s.supplier_id, body: { b2b: true } })}>Бизнес</button>
                    </div>
                  </td>
                  <td className="muted small">{time(s.last_check_at)}</td>
                  <td>
                    <label className="switch">
                      <input type="checkbox" checked={s.enabled}
                             onChange={(e) => patch.mutate({ id: s.supplier_id, body: { enabled: e.target.checked } })} />
                      <span>{s.enabled ? "вкл" : "выкл"}</span>
                    </label>
                  </td>
                  <td className="num">
                    <button className="link danger" onClick={() => confirm(`Убрать «${s.name ?? s.supplier_id}» из мониторинга?`) && remove.mutate(s.supplier_id)}>
                      Удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
