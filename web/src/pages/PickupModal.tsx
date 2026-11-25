// «Общий забор»: всё, что доехало до ПВЗ, по всем аккаунтам — одним кликом в Telegram. Окно выбирает пары «аккаунт + ПВЗ»,
// шлёт worker (нет кода на сегодня — сам сходит за ним в браузере). Отправленное остаётся в окне до забора — с отметкой,
// когда отправляли. Экран после «Отправить» строится из /pickups/sends: только отправленные пары и их статусы.
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, MSK, rub, type Account, type PickupBoard, type PickupItem, type PickupSend } from "../api";
import { Held, itemName, MaintNote, Modal, useHold } from "../accounts";

const key = (acc: number, office: number) => `${acc}:${office}`;
const pcs = (n: number) => `${n.toLocaleString("ru-RU")} шт.`;
const items = (xs: PickupItem[]) => xs.map((i) => `${itemName(i)} × ${i.n}`).join(" · ");
const code = (c: string) => c.replace(/^(\d{3})(\d{3})$/, "$1 $2");  // 112396 → 112 396
const hhmm = (iso: string) => new Date(iso).toLocaleTimeString("ru-RU", { timeZone: MSK, hour: "2-digit", minute: "2-digit" });
const sentAt = (iso: string) => new Date(iso).toLocaleString("ru-RU", { timeZone: MSK, day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit" }).replace(",", "");

function Status({ x, hold }: { x: PickupSend; hold: string | null }) {
  return <div className={`small ${x.status === "sent" ? "ok-t" : x.status === "error" ? "bad-t" : "muted"}`}>
    {x.status === "sent" ? "✅ отправлено" : x.status === "error" ? `❌ ${x.error ?? "ошибка"}`
      : hold != null ? <Held until={hold} /> : <><span className="spin" /> {x.need_code ? "получаю код…" : "отправляю…"}</>}</div>;
}

export default function PickupModal({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  const [ids, setIds] = useState<number[] | null>(null);
  if (!open) return null;
  const close = () => { setIds(null); onClose(); };  // следующее открытие — снова выбор, доска грузится заново
  return <Modal label="Общий забор" onClose={close} wide>
    {ids ? <Sent ids={ids} all={all} onClose={close} /> : <Board all={all} onClose={close} onSent={setIds} />}
  </Modal>;
}

/** После «Отправить»: только отправленные пары — ПВЗ, аккаунт, товары, статус. */
function Sent({ ids, all, onClose }: { ids: number[]; all: Account[]; onClose: () => void }) {
  const q = useQuery({ queryKey: ["pickup-sends", ids.join()],
    queryFn: () => api<PickupSend[]>(`/pickups/sends?${ids.map((i) => `ids=${i}`).join("&")}`),
    refetchInterval: (x) => (x.state.data?.some((y) => y.status === "pending") ? 1500 : false) });
  const list = q.data ?? [];
  const done = list.filter((x) => x.status !== "pending").length;
  const offices = [...new Map(list.map((x) => [x.office_id, x.address ?? `ПВЗ №${x.office_id}`])).entries()];
  const no = (id: number | null) => all.findIndex((a) => a.id === id) + 1;
  const hold = useHold("pickup");
  return (
    <>
      <h2>{q.data && done < ids.length && hold != null ? <>⏸ Ждёт окончания технических работ · отправлено {done} из {ids.length}</>
        : !q.data || done < ids.length ? <><span className="spin" /> Отправляю… {done} из {ids.length}</>
        : <>Отправлено {list.filter((x) => x.status === "sent").length} из {ids.length}</>}</h2>
      <div className="card zabor">
        {offices.map(([oid, address]) => (
          <div key={oid} className="zabor-pvz">
            <div className="zabor-head"><b>{address}</b></div>
            {list.filter((x) => x.office_id === oid).map((x) => (
              <div key={x.id} className="zabor-acc">
                <span className="zabor-name"><span><span className="muted">{no(x.account_id) || ""}</span> <b>{x.account_title ?? "аккаунт удалён"}</b></span>
                  <span className="muted small zabor-items">{items(x.items)}</span><Status x={x} hold={hold} /></span>
                <span className="zabor-kv"><span>Товаров<b>{pcs(x.items.reduce((s, i) => s + i.n, 0))}</b></span>
                  <span>Сумма<b>{rub(x.items.reduce((s, i) => s + i.sum, 0))}</b></span></span>
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="wiz-foot"><span /><button className="btn" onClick={onClose}>Готово</button></div>
    </>
  );
}

function Board({ all, onClose, onSent }: { all: Account[]; onClose: () => void; onSent: (ids: number[]) => void }) {
  const board = useQuery({ queryKey: ["pickup-board"], queryFn: () => api<PickupBoard>("/pickups/board"), gcTime: 0,
    refetchInterval: (x) => (x.state.data?.some((o) => o.accounts.some((a) => a.syncing)) ? 3000 : false) });  // коды — по одному
  const to = useQuery({ queryKey: ["pickup-to"], queryFn: () => api<{ to: number[] }>("/settings/pickup") });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<Set<number>>(new Set());  // раскрытые ПВЗ; при открытии окна все свёрнуты
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const rows = (board.data ?? []).flatMap((o) => o.accounts.map((a) => ({ o, a, k: key(a.account_id, o.office_id) })));
  const chosen = rows.filter((r) => picked.has(r.k));
  const set = (ks: string[], on: boolean) => setPicked((p) => { const n = new Set(p); ks.forEach((k) => (on ? n.add(k) : n.delete(k))); return n; });
  const no = (id: number) => all.findIndex((a) => a.id === id) + 1;
  const send = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api<{ ids: number[] }>("/pickups/send", { method: "POST",
        body: { pairs: chosen.map((r) => ({ account_id: r.a.account_id, office_id: r.o.office_id })) } });
      onSent(r.ids);
    } catch (e) { setErr(e instanceof ApiError ? e.message : "ошибка"); } finally { setBusy(false); }
  };
  const noTo = to.data && !to.data.to.length;
  const syncing = new Set(rows.filter((r) => r.a.syncing).map((r) => r.a.account_id)).size;
  const refreshCodes = async () => {
    setErr("");
    try { await api("/pickups/codes", { method: "POST" }); await board.refetch(); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "ошибка"); }
  };
  return (
    <>
      <h2>Общий забор</h2>
      <p className="muted">Товары, которые уже доехали до пунктов выдачи по всем аккаунтам. Скрытые товары не включены. Отправленные
        остаются, пока товар не заберут — у них отметка, когда отправляли. По каждой паре «аккаунт + пункт» в Telegram уйдёт отдельное
        сообщение с кодом получения.</p>
      {noTo && <p className="err">Не указано, кому отправлять. Добавьте Telegram ID в <Link to="/settings" onClick={onClose}>Настройках</Link>.</p>}
      <div className="filters">
        <button className="link" onClick={() => set(rows.map((r) => r.k), true)}>Выбрать все</button>
        <button className="link" onClick={() => setPicked(new Set())}>Снять выбор</button>
        <button className="link" onClick={() => setOpen(new Set((board.data ?? []).map((o) => o.office_id)))}>Развернуть все</button>
        <button className="link" onClick={() => setOpen(new Set())}>Свернуть все</button>
        <span className="journal-upd small muted">
          {syncing > 0 && <><span className="spin" /> обновляю коды, осталось {syncing} акк.{" "}</>}
          <button className="btn ghost sm" disabled={syncing > 0 || !rows.length} onClick={refreshCodes}>↻ Обновить коды</button>
        </span>
      </div>
      {board.isLoading ? <p className="muted"><span className="spin" /> Загрузка…</p>
        : !rows.length ? <p className="empty">Нечего забирать: в пунктах выдачи ничего не ждёт</p>
        : <div className="card zabor">
          {board.data!.map((o) => {
            const ks = o.accounts.map((a) => key(a.account_id, o.office_id));
            const n = ks.filter((k) => picked.has(k)).length;
            const on = open.has(o.office_id);
            return (
              <div key={o.office_id} className="zabor-pvz">
                <div className="zabor-head">
                  <button className="link zabor-arrow" aria-expanded={on} aria-label={`Показать аккаунты ${o.address}`}
                    onClick={() => setOpen((p) => { const x = new Set(p); on ? x.delete(o.office_id) : x.add(o.office_id); return x; })}>
                    {on ? "▾" : "▸"}</button>
                  <label>
                    <input type="checkbox" aria-label={o.address} checked={n === ks.length}
                      ref={(el) => { if (el) el.indeterminate = n > 0 && n < ks.length; }} onChange={(e) => set(ks, e.target.checked)} />
                    <b>{o.address}</b>
                  </label>
                  <span className="muted small">{o.accounts.length} акк. · {pcs(o.accounts.reduce((s, a) => s + a.n, 0))}</span>
                </div>
                {on && o.accounts.map((a) => {
                  const k = key(a.account_id, o.office_id);
                  return (
                    <label key={k} className="zabor-acc">
                      <input type="checkbox" aria-label={`${a.title}, ${o.address}`} checked={picked.has(k)}
                        onChange={(e) => set([k], e.target.checked)} />
                      <span className="zabor-name"><span><span className="muted">{no(a.account_id) || ""}</span> <b>{a.title}</b></span>
                        <span className="muted small zabor-items">{items(a.items)}</span>
                        {a.syncing ? <span className="muted small"><span className="spin" /> обновляю код…</span>
                          : a.code && <span className="small">код <b>{code(a.code)}</b>{a.code_at && <span className="muted"> · {hhmm(a.code_at)}</span>}</span>}
                        {a.sent_at && <span className="muted small">отправлено {sentAt(a.sent_at)}</span>}</span>
                      <span className="zabor-kv"><span>Товаров<b>{pcs(a.n)}</b></span><span>Сумма<b>{rub(a.sum)}</b></span></span>
                    </label>
                  );
                })}
              </div>
            );
          })}
        </div>}
      {err && <p className="err">{err}</p>}
      <MaintNote group="pickup" />
      <div className="wiz-foot">
        <span className="small muted">Выбрано: {chosen.length} из {rows.length} · {pcs(chosen.reduce((s, r) => s + r.a.n, 0))} на {rub(chosen.reduce((s, r) => s + r.a.sum, 0))}</span>
        <span className="wiz-btns">
          <button className="link" onClick={onClose}>Закрыть</button>
          <button className="btn ok" disabled={!chosen.length || busy || !!noTo} onClick={send}>Отправить в Telegram · {chosen.length}</button>
        </span>
      </div>
    </>
  );
}
