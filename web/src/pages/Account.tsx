// Страница аккаунта WB (/accounts/:id): шапка (имя, статус, обновление, прокси), плитки денег и лимита,
// вкладки: Заказы (по ПВЗ, «Готовы забрать»), История заказов, Баланс (с заметками), ВБ-баллы.
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, MSK, rub, time, type AccountFull, type BalanceOp, type Order, type OrderCancel, type Purchase, type VbOp } from "../api";
import {
  Blocked, byPvz, CancelState, CertCode, Modal, ddmm, GROUP, Limit, lines, Period, pickupText, points, STATUS, StatusDot, Thumb, Title, useImg,
  usePeriod, visible, when, type Pvz, Held, MaintNote, useHold } from "../accounts";
import CompaniesPick from "./Companies";
import LoginModal, { type Relogin } from "./LoginModal";
import { runQuote } from "./OrderModal";
import { PvzPay, toPay } from "./PayModal";

const signed = (n: number | null, unit = " ₽") => (n == null ? "—"
  : `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toLocaleString("ru-RU", { maximumFractionDigits: 2 })}${unit}`);
const wbLink = (nm: number | null) => `https://www.wildberries.ru/catalog/${nm}/detail.aspx`;

/** ↻ «Обновить»: опрос сам глотает ошибки — запоминаем, что было при нажатии, и по итогу говорим, обновилось ли. */
function useRefresh(a: AccountFull | undefined) {
  const qc = useQueryClient();
  const [pressed, setPressed] = useState<{ at: number; stateAt: string | null } | null>(null);
  const [note, setNote] = useState("");
  const poll = useMutation({
    mutationFn: () => api(`/accounts/${a!.id}/poll`, { method: "POST" }),
    onMutate: () => { setNote(""); setPressed({ at: Date.now(), stateAt: a!.state_at }); },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
    onError: (e: unknown) => { setPressed(null); setNote(e instanceof ApiError ? e.message : "ошибка"); },
  });
  useEffect(() => {
    if (!a || !pressed || a.polling || poll.isPending) return;
    if (a.state_at === pressed.stateAt) {
      const why = a.status === "relogin" ? "нужен вход"
        : a.proxy_fail_at && new Date(a.proxy_fail_at).getTime() > pressed.at ? "прокси не ответил" : "";
      setNote(why ? `не обновилось — ${why}` : "не обновилось");
    }
    setPressed(null);
  }, [a?.polling, a?.state_at, a?.status, poll.isPending]);
  return { poll, note };
}

/** Визит в браузере аккаунта (~30 с): история баланса. Пока идёт — browser_sync. Код получения — HTTP-опросом. */
function useSync(a: AccountFull) {
  const qc = useQueryClient();
  const [err, setErr] = useState("");
  const sync = useMutation({
    mutationFn: () => api(`/accounts/${a.id}/sync`, { method: "POST" }),
    onMutate: () => setErr(""),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts", a.id] }),
    onError: (e: unknown) => setErr(e instanceof ApiError ? e.message : "ошибка"),
  });
  return { start: () => sync.mutate(), busy: a.browser_sync || sync.isPending, err };
}

// ---------- Заказы ----------
/** Отмена позиций группы ПВЗ (и в пути, и в ПВЗ — что взять, решает WB): строки «× N» с галочками → очередь worker'а;
 *  итог — по факту (архив WB / свежие заказы), как у отмены из окна заказа. */
function PvzCancel({ a, g, onClose }: { a: AccountFull; g: Pvz; onClose: () => void }) {
  const qc = useQueryClient();
  const rows = lines(visible(g.orders));
  const img = useImg();  // те же картинки, что на странице — из кэша браузера
  const [off, setOff] = useState<Set<string>>(new Set());  // снятые галочки (ключи строк)
  const [id, setId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const chosen = rows.filter((l) => !off.has(l.key)).flatMap((l) => l.orders);
  const sum = chosen.reduce((s, o) => s + (o.price ?? 0), 0);
  const allBox = useRef<HTMLInputElement>(null);
  useEffect(() => { if (allBox.current) allBox.current.indeterminate = off.size > 0 && off.size < rows.length; });
  const q = useQuery({ queryKey: ["order-cancel", id], enabled: id != null,
    queryFn: () => api<OrderCancel[]>(`/order-cancels?ids=${id}`).then((x) => x[0]),
    refetchInterval: (x) => { const c = x.state.data; return c && (c.status === "error" || (c.status === "asked" && !c.cancel?.asked)) ? false : 2000; } });
  const c = q.data;
  const hold = useHold("money");
  useEffect(() => { if (c && c.status !== "pending") qc.invalidateQueries({ queryKey: ["accounts"] }); },
    [c?.status, c?.cancel?.cancelled, c?.cancel?.failed]);
  const go = async () => {
    if (!confirm(`Отменить ${chosen.length} шт. на ${rub(sum)} на аккаунте ${a.title}?`
      + (chosen.some((o) => o.paid) ? "\n\nОплаченные — деньги вернутся на Кошелёк." : ""))) return;
    setError("");
    try { setId((await api<{ id: number }>("/order-cancels", { method: "POST", body: { account_id: a.id, rids: chosen.map((o) => o.rid) } })).id); }
    catch (e) { setError(e instanceof ApiError ? e.message : "ошибка"); }
  };
  return (
    <Modal label="Отменить товары" onClose={onClose}>
      <h2>{a.title} · {g.address}</h2>
      <p className="muted small">Можно отменить и то, что едет, и то, что уже в пункте выдачи — WB сам решит, что успеет отменить.</p>
      <ul className="pay-items">
        {rows.length > 1 && <li className="pay-all"><label>
          <input ref={allBox} type="checkbox" checked={!off.size} disabled={id != null}
            onChange={() => setOff(off.size ? new Set() : new Set(rows.map((l) => l.key)))} /> Выбрать все</label></li>}
        {rows.map(({ key, orders, first: o }) => (
          <li key={key}><label>
            <input type="checkbox" checked={!off.has(key)} disabled={id != null} onChange={() => {
              const s = new Set(off); s.has(key) ? s.delete(key) : s.add(key); setOff(s);
            }} />
            <Thumb src={img(o.nm_id)} />
            <span>{o.name ?? `арт. ${o.nm_id}`}{orders.length > 1 && <b> × {orders.length}</b>}{" "}
              <span className={`pill g${o.group_id ?? 0}`}>{o.status_name ?? GROUP[o.group_id ?? 0] ?? "—"}</span>{" "}
              <span className={o.paid === false ? "badge out" : "badge in"}>{o.paid === false ? "Не оплачен" : o.paid ? "Оплачен" : "оплата ?"}</span></span>
            <b className="num">{rub(o.price)}{orders.length > 1 && <span className="muted small"> за шт.</span>}</b></label></li>
        ))}
      </ul>
      {c?.status === "pending" && hold != null ? <div className="small"><Held until={hold} /></div>
        : c?.status === "pending" || (id != null && !c) ? <div className="small"><span className="spin" /> Отменяю…</div>
        : c?.status === "error" ? <div className="small bad-t">❌ {c.error}</div>
        : c?.status === "asked" && <>
          {c.started.length < c.rids.length && <div className="small up-t">WB начал отмену {c.started.length} из {c.rids.length} шт.;
            остальные не взял — проверьте на сайте WB</div>}
          {c.cancel && <CancelState c={c.cancel} />}
        </>}
      {error && <p className="err">{error}</p>}
      <MaintNote group="money" />
      <div className="wiz-foot">
        <span className="small muted">Выбрано: {chosen.length} шт. на {rub(sum)}</span>
        <span className="wiz-btns">
          <button className="link" onClick={onClose}>Закрыть</button>
          <button className="btn bad" disabled={!chosen.length || id != null} onClick={go}>Отменить {chosen.length} шт.</button>
        </span>
      </div>
    </Modal>
  );
}

function PickupButton({ g, a }: { g: Pvz; a: AccountFull }) {
  const qc = useQueryClient();
  const [asked, setAsked] = useState(false);  // ждём код, который запросили отсюда
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState("");
  const get = useMutation({  // кода на сегодня нет — внеочередной HTTP-опрос: код берётся всегда, за секунды (otp/get)
    mutationFn: () => api(`/accounts/${a.id}/poll`, { method: "POST" }),
    onMutate: () => setErr(""),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts", a.id] }),
    onError: (e: unknown) => setErr(e instanceof ApiError ? e.message : "ошибка"),
  });
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(pickupText(g, a, a.pickup_code!));
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch { alert(pickupText(g, a, a.pickup_code!)); }  // буфер недоступен (не https) — покажем текст
  };
  if (a.pickup_code) return (  // буфер пишется надёжно только по клику — поэтому без автокопирования после ожидания
    <button className="btn ok" onClick={copy}>{copied ? "Скопировано ✓" : asked ? "Скопировать" : "Готовы забрать"}</button>);
  if (a.polling || get.isPending) return <span className="muted small"><span className="spin" /> Получаю код…</span>;
  return (
    <>
      {asked && <span className="up-t small">{err || "код не получен — попробуйте ещё раз"} </span>}
      <button className="btn ok" onClick={() => { setAsked(true); get.mutate(); }}>Готовы забрать</button>
    </>
  );
}

function OrdersTab({ a }: { a: AccountFull }) {
  const qc = useQueryClient();
  const img = useImg();
  const [shown, setShown] = useState<Set<number>>(new Set());  // ПВЗ, где показываем скрытые
  const [paying, setPaying] = useState<number | null>(null);  // ПВЗ, где открыто окно оплаты
  const [cancelling, setCancelling] = useState<Pvz | null>(null);  // снимок группы: после отмены позиции пропадут из заказов
  // rid с точками (eAe.dc18….0.0) — в URL кодируем; «Скрыть» строки = все её позиции
  const toggle = useMutation({
    mutationFn: (os: Order[]) => Promise.all(os.map((o) => api(`/accounts/${a.id}/orders/${encodeURIComponent(o.rid)}`,
      { method: "PATCH", body: { hidden: !o.hidden } }))),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });
  if (!a.orders.length) return <div className="card empty">Активных заказов нет</div>;
  const retail = a.orders.filter((o) => !o.company_id);  // 🏢 заказы компаний — своими блоками ниже
  return (
    <div className="pvz-list">
      {byPvz(retail).map((g) => {
        const vis = visible(g.orders);
        const hidden = g.orders.length - vis.length;
        const ready = vis.filter((o) => o.group_id === 5).length;
        const going = vis.length - ready;  // всё, что не в ПВЗ, — «в пути», как transit
        const rows = lines(shown.has(g.id) ? g.orders : vis);
        const due = toPay(a, g.orders).reduce((s, o) => s + (o.price ?? 0), 0);
        return (
          <div key={g.id} className="card pvz-group">
            <div className="pvz-head">
              <div><b>{g.address}</b>{g.hours && <span className="muted small"> · {g.hours}</span>}
                <div className="muted small">{vis.length} шт.{ready > 0 && ` · ${ready} ждут в ПВЗ`}{going > 0 && ` · ${going} в пути`}</div></div>
              {ready > 0 && <span className="badge in">Можно забирать</span>}
            </div>
            {rows.map(({ key, orders, first: o }) => (
              <div key={key} className={`ord${o.hidden ? " off" : ""}`}>
                <Thumb src={img(o.nm_id)} />
                <div className="ord-main">
                  <div>{o.name ?? `арт. ${o.nm_id}`}{orders.length > 1 && <b> × {orders.length}</b>}</div>
                  <div className="ord-tags">
                    <span className={o.paid === false ? "badge out" : "badge in"}>{o.paid === false ? "Не оплачен" : o.paid ? "Оплачен" : "оплата ?"}</span>
                    <span className={`pill g${o.group_id ?? 0}`}>{o.status_name ?? GROUP[o.group_id ?? 0] ?? "—"}</span>
                    {o.expiry_at ? <span className="muted small">хранение до {ddmm(o.expiry_at)}</span>
                      : o.delivery_at && <span className="muted small">прибытие {ddmm(o.delivery_at)}</span>}
                  </div>
                  <div className="small">
                    <a href={wbLink(o.nm_id)} target="_blank" rel="noreferrer">Найти на WB</a>{" · "}
                    <button className="link" disabled={toggle.isPending} onClick={() => toggle.mutate(orders)}>{o.hidden ? "Вернуть" : "Скрыть"}</button>
                  </div>
                </div>
                <div className="ord-price num">{rub(o.price)}{orders.length > 1 && <div className="muted small">за шт.</div>}</div>
              </div>
            ))}
            <div className="pvz-foot">
              <span>Итого: <b>{rub(vis.reduce((s, o) => s + (o.price ?? 0), 0))}</b> · {vis.length} шт.</span>
              {hidden > 0 && <span className="muted small">Скрытых: {hidden} · <button className="link" onClick={() => {
                const s = new Set(shown); s.has(g.id) ? s.delete(g.id) : s.add(g.id); setShown(s);
              }}>{shown.has(g.id) ? "убрать" : "показать"}</button></span>}
              {vis.length > 0 && <button className="btn ghost bad-t" onClick={() => setCancelling(g)}>Отменить</button>}
              {due > 0 && <button className="btn" onClick={() => setPaying(g.id)}>Оплатить {rub(due)}</button>}
              {ready > 0 && <PickupButton g={g} a={a} />}
            </div>
          </div>
        );
      })}
      <CompanyOrders a={a} />
      {paying != null && byPvz(retail).some((g) => g.id === paying) &&
        <PvzPay a={a} g={byPvz(retail).find((g) => g.id === paying)!} onClose={() => setPaying(null)} />}
      {cancelling && <PvzCancel a={a} g={cancelling} onClose={() => setCancelling(null)} />}
    </div>
  );
}

/** 🏢 Счёт = заказ компании (все его rids; order_uid — средняя часть rid «eAl.<uid>.0.0»). */
type Invoice = { uid: string; orders: Order[]; first: Order };
const invoices = (orders: Order[]): Invoice[] => {
  const m = new Map<string, Order[]>();
  for (const o of orders) { const uid = o.rid.split(".")[1] ?? o.rid; m.set(uid, [...(m.get(uid) ?? []), o]); }
  return [...m].map(([uid, os]) => ({ uid, orders: os, first: os[0] }));
};
const until = (iso: string) => new Date(iso).toLocaleString("ru-RU", { timeZone: MSK, day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit" });

/** 🏢 «Скачать счёт» (прямая ссылка из заказов; нет её — worker берёт ZIP у WB) и «Отменить счёт» (только неоплаченный,
 *  весь счёт; итог — счёт пропал из заказов). */
function InvoiceActions({ a, inv }: { a: AccountFull; inv: Invoice }) {
  const qc = useQueryClient();
  const o = inv.first;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [id, setId] = useState<number | null>(null);
  const q = useQuery({ queryKey: ["order-cancel", id], enabled: id != null,
    queryFn: () => api<OrderCancel[]>(`/order-cancels?ids=${id}`).then((x) => x[0]),
    refetchInterval: (x) => { const c = x.state.data; return c && (c.status === "error" || (c.status === "asked" && !c.cancel?.asked)) ? false : 2000; } });
  const c = q.data;
  const hold = useHold("money");
  useEffect(() => { if (c && c.status !== "pending") qc.invalidateQueries({ queryKey: ["accounts"] }); },
    [c?.status, c?.cancel?.cancelled]);
  const fail = (e: unknown) => setErr(e instanceof ApiError || e instanceof Error ? e.message : "ошибка");
  const download = async () => {
    setBusy(true); setErr("");
    try { window.location.assign(`/api/order/invoice/${(await runQuote<unknown>("/order/invoice", { account_id: a.id, rid: o.rid })).id}`); }
    catch (e) { fail(e); } finally { setBusy(false); }
  };
  const cancel = async () => {
    const sum = inv.orders.reduce((s, x) => s + (x.price ?? 0), 0);
    if (!confirm(`Отменить весь счёт: ${o.name ?? `арт. ${o.nm_id}`} × ${inv.orders.length} на ${rub(sum)}?\n\nWB отменит заказ целиком.`)) return;
    setErr("");
    try { setId((await api<{ id: number }>("/order-cancels", { method: "POST", body: { account_id: a.id, rids: inv.orders.map((x) => x.rid) } })).id); }
    catch (e) { fail(e); }
  };
  return (
    <div className="small inv-acts">
      {o.invoice_url ? <a href={o.invoice_url} target="_blank" rel="noreferrer">Скачать счёт</a>
        : <button className="link" disabled={busy} onClick={download}>{busy ? <><span className="spin" /> Готовлю счёт…</> : "Скачать счёт"}</button>}
      {o.paid === false && id == null && <> · <button className="link danger" onClick={cancel}>Отменить счёт</button></>}
      {c?.status === "pending" && hold != null ? <div><Held until={hold} /></div>
        : c?.status === "pending" || (id != null && !c) ? <div><span className="spin" /> Отменяю счёт…</div>
        : c?.status === "error" ? <div className="bad-t">❌ {c.error}</div>
        : c?.status === "asked" && c.cancel && <CancelState c={c.cancel} />}
      {err && <div className="err">{err}</div>}
    </div>
  );
}

/** 🏢 Заказы компаний номера: по компании, внутри — по счетам. Не оплачен — «ждёт оплаты по счёту до …»;
 *  оплачен — статус доставки (❓ WB для бизнес-rids его пока не проверяли — «—»). */
function CompanyOrders({ a }: { a: AccountFull }) {
  const img = useImg();
  const mine = a.orders.filter((o) => o.company_id);
  if (!mine.length) return null;
  const ids = [...new Set(mine.map((o) => o.company_id!))];
  return <>{ids.map((cid) => {
    const c = a.companies.find((x) => x.id === cid);
    const os = mine.filter((o) => o.company_id === cid);
    return (
      <div key={cid} className="card pvz-group co-orders">
        <div className="pvz-head"><div><b>🏢 {c?.name ?? c?.full_name ?? "Компания (скрыта)"}</b>
          {c?.inn && <span className="muted small"> · ИНН {c.inn}</span>}
          <div className="muted small">{os.length} шт. · заказы по счёту</div></div></div>
        {invoices(os).map(({ uid, orders, first: o }) => (
          <div key={uid} className="ord">
            <Thumb src={img(o.nm_id)} />
            <div className="ord-main">
              <div>{o.name ?? `арт. ${o.nm_id}`}{orders.length > 1 && <b> × {orders.length}</b>}</div>
              <div className="ord-tags">
                <span className={o.paid === false ? "badge out" : "badge in"}>{o.paid === false ? "Не оплачен" : o.paid ? "Оплачен" : "оплата ?"}</span>
                {o.paid === false ? <span className="pill">{o.status_name ?? "Ждёт оплаты по счёту"}{o.pay_until && ` до ${until(o.pay_until)}`}</span>
                  : <span className={`pill g${o.group_id ?? 0}`}>{o.status_name ?? GROUP[o.group_id ?? 0] ?? "—"}</span>}
                {o.dst_office_id != null && <span className="muted small">{o.office_address ?? `ПВЗ №${o.dst_office_id}`}</span>}
              </div>
            </div>
            <div className="ord-price num">{rub(orders.reduce((s, x) => s + (x.price ?? 0), 0))}
              {orders.length > 1 && <div className="muted small">{rub(o.price)} за шт.</div>}</div>
            <InvoiceActions a={a} inv={{ uid, orders, first: o }} />
          </div>
        ))}
      </div>
    );
  })}</>;
}

// ---------- История заказов ----------
function HistoryTab({ a }: { a: AccountFull }) {
  const img = useImg();
  const period = usePeriod(7);  // по умолчанию — 7 дней, включая сегодня
  const { from, to } = period;
  const q = useQuery({ queryKey: ["accounts", a.id, "purchases", from, to],
    queryFn: () => api<Purchase[]>(`/accounts/${a.id}/purchases?${new URLSearchParams({ ...(from && { from }), ...(to && { to }) })}`) });
  // одна строка — один товар, забранный в одном ПВЗ в одну минуту
  const groups = new Map<string, { p: Purchase; n: number; sum: number }>();
  for (const p of q.data ?? []) {
    const k = [p.nm_id, p.office_id, p.dt?.slice(0, 16), p.price].join("|");
    const g = groups.get(k) ?? { p, n: 0, sum: 0 };
    g.n += 1; g.sum += p.price ?? 0;
    groups.set(k, g);
  }
  const all = q.data ?? [];
  return (
    <div className="card">
      <div className="filters">
        <Period p={period} />
        <span className="sum">Забрано: <b>{all.length} шт.</b> на <b>{rub(all.reduce((s, p) => s + (p.price ?? 0), 0))}</b></span>
      </div>
      {q.isLoading ? <p className="muted">Загрузка…</p> : !all.length ? <p className="empty">За этот период ничего не забирали</p> : (
        <div className="scroll">
          <table>
            <thead><tr><th>Забрали</th><th>Пункт выдачи</th><th>Товар</th><th className="num">Кол-во</th>
              <th className="num">Цена за шт.</th><th className="num">Сумма</th></tr></thead>
            <tbody>
              {[...groups.values()].map(({ p, n, sum }) => (
                <tr key={p.rid}>
                  <td className="nowrap">{when(p.dt)}</td>
                  <td className="small">{p.office_address ?? (p.office_id ? `ПВЗ №${p.office_id}` : "—")}</td>
                  <td><div className="prod"><Thumb src={img(p.nm_id)} />
                    <a href={wbLink(p.nm_id)} target="_blank" rel="noreferrer">{p.name ?? `арт. ${p.nm_id}`}</a></div></td>
                  <td className="num">{n}</td>
                  <td className="num">{rub(p.price)}</td>
                  <td className="num"><b>{rub(sum)}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------- Баланс ----------
function Note({ a, op }: { a: AccountFull; op: BalanceOp }) {
  const qc = useQueryClient();
  const [text, setText] = useState(op.note ?? "");
  const [state, setState] = useState("");
  const save = useMutation({
    mutationFn: () => api(`/accounts/${a.id}/balance/${encodeURIComponent(op.tx_id)}/note`, { method: "PUT", body: { text } }),
    onSuccess: () => { setState("сохранено"); qc.invalidateQueries({ queryKey: ["accounts", a.id, "balance"] }); },
    onError: () => setState("не сохранилось"),
  });
  return (
    <span className="note">
      <input placeholder="заметка" maxLength={1000} value={text} onChange={(e) => { setText(e.target.value); setState(""); }}
        onBlur={() => text.trim() !== (op.note ?? "") && save.mutate()} />
      {state && <span className={`small ${state === "сохранено" ? "muted" : "bad-t"}`}>{state}</span>}
    </span>
  );
}

function BalanceTab({ a, sync }: { a: AccountFull; sync: ReturnType<typeof useSync> }) {
  const q = useQuery({ queryKey: ["accounts", a.id, "balance"], queryFn: () => api<BalanceOp[]>(`/accounts/${a.id}/balance`) });
  const period = usePeriod(null);  // как на макете — «Всё время»
  const [gifts, setGifts] = useState(false);
  // все накопленные операции уже загружены (их немного) — режем на клиенте
  const ops = (q.data ?? []).filter((op) => period.has(op.dt) && (!gifts || op.source_type === "giftcards"));
  const income = ops.reduce((s, op) => s + Math.max(0, Number(op.amount ?? 0)), 0);
  const spent = ops.reduce((s, op) => s + Math.max(0, -Number(op.amount ?? 0)), 0);
  return (
    <div className="card">
      <div className="filters">
        <Period p={period} />
        <label className="small"><input type="checkbox" checked={gifts} onChange={(e) => setGifts(e.target.checked)} /> Пополнение сертификатами</label>
        <span className="sum">Пополнения: <b className="ok-t">{rub(income)}</b> · Списания: <b>{rub(spent)}</b></span>
      </div>
      <div className="filters bal-hint">
        <span className="muted small">У каждой операции есть заметка: у пополнения впишите, откуда деньги, у списания — на что ушли</span>
        {sync.busy ? <span className="muted small"><span className="spin" /> Обновляю… (~30 с)</span>
          : <button className="btn ghost sm" onClick={sync.start}>↻ Обновить</button>}
      </div>
      {sync.err && <p className="err">{sync.err}</p>}
      {q.isLoading ? <p className="muted">Загрузка…</p>
        : !(q.data ?? []).length ? <p className="empty">Операций пока нет — нажмите «Обновить» (браузер ~30 с)</p>
        : !ops.length ? <p className="empty">Нет операций за период</p> : (
        <div className="ops">
          {ops.map((op) => (
            <div key={op.tx_id} className="op">
              <span className={`op-ico ${op.op_type === 0 ? "in" : "out"}`}>{op.op_type === 0 ? "↑" : "↓"}</span>
              <div className="op-main"><div>{op.name ?? "—"}</div><div className="muted small">{when(op.dt)}</div>
                {op.cert_code && <div className="cert-m"><CertCode code={op.cert_code} /></div>}</div>
              <span className="op-cert">{op.cert_code && <CertCode code={op.cert_code} />}</span>
              <Note a={a} op={op} />
              <b className={`num op-sum ${(op.amount ?? 0) > 0 ? "ok-t" : ""}`}>{signed(op.amount)}</b>
            </div>
          ))}
        </div>
      )}
      <p className="muted small">WB отдаёт 20 последних операций — старые копятся здесь.</p>
    </div>
  );
}

// ---------- ВБ-баллы ----------
function VbTab({ a }: { a: AccountFull }) {
  const q = useQuery({ queryKey: ["accounts", a.id, "vb"], queryFn: () => api<VbOp[]>(`/accounts/${a.id}/vb`) });
  if (!(q.data ?? []).length) return <div className="card empty">{q.isLoading ? "Загрузка…" : "Операций с баллами пока нет"}</div>;
  return (
    <div className="card ops">
      {q.data!.map((op) => {
        const plus = op.operation === 1;
        return (
          <div key={op.tx_id} className="op">
            <span className={`op-ico ${plus ? "in" : "out"}`}>{plus ? "↑" : "↓"}</span>
            <div className="op-main"><div>{op.title ?? "—"}</div>
              <div className="muted small">{when(op.dt)}{op.description && ` · ${op.description}`}</div></div>
            <b className={`num op-sum ${plus ? "ok-t" : ""}`}>{signed(plus ? op.amount : -(op.amount ?? 0), " б.")}</b>
          </div>
        );
      })}
    </div>
  );
}

const TABS = [["orders", "Заказы"], ["history", "История заказов"], ["balance", "Баланс"], ["vb", "ВБ-баллы"],
  ["companies", "Компании"]] as const;

export default function Account() {
  const id = Number(useParams().id);
  const nav = useNavigate();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const tab = TABS.find(([k]) => k === params.get("tab"))?.[0] ?? "orders";
  const q = useQuery({ queryKey: ["accounts", id], queryFn: () => api<AccountFull>(`/accounts/${id}`), retry: false,
    refetchInterval: (q) => (q.state.data?.polling || q.state.data?.browser_sync ? 2000 : 60_000) });
  const a = q.data;
  const { poll, note } = useRefresh(a);
  const [relogin, setRelogin] = useState<Relogin | null>(null);
  const del = useMutation({
    mutationFn: () => api(`/accounts/${id}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["accounts"] }); nav("/accounts"); },
  });
  const wasSync = useRef(false);
  useEffect(() => {  // визит закончился — операции баланса могли прибавиться
    if (wasSync.current && !a?.browser_sync) qc.invalidateQueries({ queryKey: ["accounts", id, "balance"] });
    wasSync.current = !!a?.browser_sync;
  }, [a?.browser_sync]);
  const back = <Link to="/accounts" className="back">← Все аккаунты</Link>;
  if (q.isError) return <section>{back}<div className="card empty">Аккаунт не найден</div></section>;
  if (!a) return <section>{back}<p className="muted">Загрузка…</p></section>;
  const counts = { orders: visible(a.orders).length, history: a.counts.purchases, balance: a.counts.balance, vb: a.counts.vb,
    companies: a.companies?.length ?? 0 };  // 🏢 включённые
  return (
    <section>
      {back}
      <div className="acc-top">
        <div className="acc-title">
          <StatusDot a={a} /><Title a={a} />
          {a.wallet_pct != null && <span className="badge in">Кошелёк −{a.wallet_pct}%</span>}
          <span className={`pill ${a.status === "active" ? "ok" : "bad"}`}>{STATUS[a.status] ?? a.status}</span>
          <Blocked a={a} />
          {(a.status === "relogin" || a.site_lost) && <button className="btn sm" onClick={() => setRelogin({ a, n: Date.now() })}>Войти заново</button>}
        </div>
        <div className="acc-actions">
          <button className="btn ghost" disabled={a.polling || poll.isPending} onClick={() => poll.mutate()}>
            {a.polling || poll.isPending ? <><span className="spin" /> Обновляю…</> : "↻ Обновить"}</button>
          <button className="link danger" onClick={() => confirm(
            `Удалить аккаунт ${[a.title, a.phone ?? a.phone_mask].join(" ")}?\n\n`
            + "Сессия удалится, бизнес-цены и заказы под ним перестанут обновляться. Вход — заново по коду.") && del.mutate()}>Удалить</button>
        </div>
      </div>
      <div className="muted small acc-sub">
        {a.phone ?? a.phone_mask} · {a.kind === "business" ? "Бизнес" : "Обычный"} · обновлён {time(a.state_at)} ·{" "}
        {a.proxy ? `прокси ${a.proxy.label} · ${a.proxy.exit_ip ?? a.proxy.server}` : <span className="up-t">без своего прокси — IP мониторинга</span>}
        {note && <span className="up-t"> · {note}</span>}
      </div>
      {a.site_lost && a.status === "active" && <div className="small bad-t">{a.visit_error}: история
        баланса не обновляется (лимит, заказы и код получения — обновляются)</div>}
      {a.wallet_blocked && <div className="small bad-t">WB наложил ограничение на Кошелёк — оплата им недоступна</div>}
      {a.proxy_down && <div className="small up-t">прокси не ответил {a.proxy_fails} раз подряд, последний {time(a.proxy_fail_at)}</div>}
      <div className="tiles acc-tiles">
        <div className="tile"><b className="money">{rub(a.funds)}</b><span>Кошелёк WB</span></div>
        <div className="tile"><b>{points(a.vb_points)}</b><span>ВБ-баллы</span></div>
        <div className="tile wide"><span>Лимит скидки</span><Limit a={a} /></div>
      </div>
      <div className="tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""}
            onClick={() => setParams(k === "orders" ? {} : { tab: k }, { replace: true })}>
            {label} <span className="cnt">{counts[k]}</span></button>
        ))}
      </div>
      <TabBody a={a} tab={tab} />
      <LoginModal open={!!relogin} onClose={() => setRelogin(null)} relogin={relogin} />
    </section>
  );
}

function TabBody({ a, tab }: { a: AccountFull; tab: string }) {
  const sync = useSync(a);  // «Обновить» баланса — визит в браузере
  return tab === "history" ? <HistoryTab a={a} /> : tab === "balance" ? <BalanceTab a={a} sync={sync} />
    : tab === "vb" ? <VbTab a={a} /> : tab === "companies" ? <div className="card"><CompaniesPick accountId={a.id} /></div>
    : <OrdersTab a={a} />;
}
