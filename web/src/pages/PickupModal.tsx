// «Общий забор»: всё, что доехало до ПВЗ, по всем аккаунтам — одним кликом в Telegram. Окно выбирает пары «аккаунт + ПВЗ»,
// шлёт worker (нет кода на сегодня — сам сходит за ним в браузере), окно опрашивает статусы.
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, rub, type Account, type PickupBoard, type PickupSend } from "../api";
import { Modal } from "../accounts";

const key = (acc: number, office: number) => `${acc}:${office}`;
const pcs = (n: number) => `${n.toLocaleString("ru-RU")} шт.`;

function Status({ x }: { x?: PickupSend }) {
  if (!x) return null;
  return <div className={`small ${x.status === "sent" ? "ok-t" : x.status === "error" ? "bad-t" : "muted"}`}>
    {x.status === "sent" ? "✅ отправлено" : x.status === "error" ? `❌ ${x.error ?? "ошибка"}`
      : <><span className="spin" /> {x.need_code ? "получаю код…" : "отправляю…"}</>}</div>;
}

export default function PickupModal({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  if (!open) return null;
  return <Modal label="Общий забор" onClose={onClose} wide><Board all={all} onClose={onClose} /></Modal>;
}

function Board({ all, onClose }: { all: Account[]; onClose: () => void }) {
  const board = useQuery({ queryKey: ["pickup-board"], queryFn: () => api<PickupBoard>("/pickups/board"), gcTime: 0 });
  const to = useQuery({ queryKey: ["pickup-to"], queryFn: () => api<{ to: number[] }>("/settings/pickup") });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [ids, setIds] = useState<number[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const sends = useQuery({ queryKey: ["pickup-sends", ids?.join()], enabled: !!ids,
    queryFn: () => api<PickupSend[]>(`/pickups/sends?${ids!.map((i) => `ids=${i}`).join("&")}`),
    refetchInterval: (q) => (q.state.data?.some((x) => x.status === "pending") ? 1500 : false) });
  const byKey = new Map((sends.data ?? []).map((x) => [key(x.account_id ?? 0, x.office_id), x]));
  const rows = (board.data ?? []).flatMap((o) => o.accounts.map((a) => ({ o, a, k: key(a.account_id, o.office_id) })));
  const chosen = rows.filter((r) => picked.has(r.k));
  const set = (ks: string[], on: boolean) => setPicked((p) => { const n = new Set(p); ks.forEach((k) => (on ? n.add(k) : n.delete(k))); return n; });
  const no = (id: number) => all.findIndex((a) => a.id === id) + 1;
  const done = sends.data?.filter((x) => x.status !== "pending").length ?? 0;
  const send = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api<{ ids: number[] }>("/pickups/send", { method: "POST",
        body: { pairs: chosen.map((r) => ({ account_id: r.a.account_id, office_id: r.o.office_id })) } });
      setIds(r.ids);
    } catch (e) { setErr(e instanceof ApiError ? e.message : "ошибка"); } finally { setBusy(false); }
  };
  const noTo = to.data && !to.data.to.length;
  return (
    <>
      <h2>{!ids ? "Общий забор" : done < ids.length ? <><span className="spin" /> Отправляю… {done} из {ids.length}</>
        : <>Отправлено {sends.data?.filter((x) => x.status === "sent").length} из {ids.length}</>}</h2>
      <p className="muted">Товары, которые уже доехали до пунктов выдачи по всем аккаунтам. Скрытые товары и уже отправленные
        заборы не включены. По каждой паре «аккаунт + пункт» в Telegram уйдёт отдельное сообщение с кодом получения.</p>
      {noTo && <p className="err">Не указано, кому отправлять. Добавьте Telegram ID в <Link to="/settings" onClick={onClose}>Настройках</Link>.</p>}
      {!ids && <div className="filters">
        <button className="link" onClick={() => set(rows.map((r) => r.k), true)}>Выбрать все</button>
        <button className="link" onClick={() => setPicked(new Set())}>Снять выбор</button>
      </div>}
      {board.isLoading ? <p className="muted"><span className="spin" /> Загрузка…</p>
        : !rows.length ? <p className="empty">Нечего забирать: в пунктах выдачи ничего не ждёт или всё уже отправлено</p>
        : <div className="card zabor">
          {board.data!.map((o) => {
            const ks = o.accounts.map((a) => key(a.account_id, o.office_id));
            const n = ks.filter((k) => picked.has(k)).length;
            return (
              <div key={o.office_id} className="zabor-pvz">
                <label className="zabor-head">
                  {!ids && <input type="checkbox" aria-label={o.address} checked={n === ks.length}
                    ref={(el) => { if (el) el.indeterminate = n > 0 && n < ks.length; }} onChange={(e) => set(ks, e.target.checked)} />}
                  <b>{o.address}</b>
                  <span className="muted small">{o.accounts.length} акк. · {pcs(o.accounts.reduce((s, a) => s + a.n, 0))}</span>
                </label>
                {o.accounts.map((a) => {
                  const k = key(a.account_id, o.office_id);
                  if (ids && !byKey.has(k) && !picked.has(k)) return null;  // после отправки — только отправленные
                  return (
                    <label key={k} className="zabor-acc">
                      {!ids && <input type="checkbox" aria-label={`${a.title}, ${o.address}`} checked={picked.has(k)}
                        onChange={(e) => set([k], e.target.checked)} />}
                      <span className="zabor-name"><span><span className="muted">{no(a.account_id) || ""}</span> <b>{a.title}</b></span>
                        <span className="muted small zabor-items">{a.items.map((i) => `${i.name} × ${i.n}`).join(" · ")}</span>
                        <Status x={byKey.get(k)} /></span>
                      <span className="zabor-kv"><span>Товаров<b>{pcs(a.n)}</b></span><span>Сумма<b>{rub(a.sum)}</b></span></span>
                    </label>
                  );
                })}
              </div>
            );
          })}
        </div>}
      {err && <p className="err">{err}</p>}
      <div className="wiz-foot">
        <span className="small muted">{!ids && `Выбрано: ${chosen.length} из ${rows.length} · ${pcs(chosen.reduce((s, r) => s + r.a.n, 0))} на ${rub(chosen.reduce((s, r) => s + r.a.sum, 0))}`}</span>
        <span className="wiz-btns">
          <button className={ids ? "btn" : "link"} onClick={onClose}>{ids ? "Готово" : "Закрыть"}</button>
          {!ids && <button className="btn ok" disabled={!chosen.length || busy || !!noTo} onClick={send}>
            Отправить в Telegram · {chosen.length}</button>}
        </span>
      </div>
    </>
  );
}
