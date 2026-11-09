// Страница аккаунта WB (/accounts/:id): шапка (имя, статус, обновление, прокси), плитки денег и лимита,
// вкладки: Заказы (по ПВЗ, «Готовы забрать»), История заказов, Баланс (с заметками), ВБ-баллы.
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, time, type AccountFull, type BalanceOp, type Order, type Purchase, type VbOp } from "../api";
import {
  byPvz, ddmm, GROUP, Limit, lines, pickupText, points, STATUS, StatusDot, Thumb, Title, useImg, visible, type Pvz,
} from "../accounts";
import LoginModal, { type Relogin } from "./LoginModal";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("ru-RU",
  { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");
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

/** Визит в браузере аккаунта (~30 с): код получения + история баланса. Пока идёт — browser_sync. */
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
function PickupButton({ g, a, sync }: { g: Pvz; a: AccountFull; sync: ReturnType<typeof useSync> }) {
  const [asked, setAsked] = useState(false);  // ждём код, который запросили отсюда
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(pickupText(g, a, a.pickup_code!));
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch { alert(pickupText(g, a, a.pickup_code!)); }  // буфер недоступен (не https) — покажем текст
  };
  if (a.pickup_code) return (  // буфер пишется надёжно только по клику — поэтому без автокопирования после ожидания
    <button className="btn ok" onClick={copy}>{copied ? "Скопировано ✓" : asked ? "Скопировать" : "Готовы забрать"}</button>);
  if (sync.busy) return <span className="muted small"><span className="spin" /> Получаю код… (~30 с)</span>;
  return (
    <>
      {asked && <span className="up-t small">код не получен — попробуйте ещё раз </span>}
      <button className="btn ok" onClick={() => { setAsked(true); sync.start(); }}>Готовы забрать</button>
    </>
  );
}

function OrdersTab({ a, sync }: { a: AccountFull; sync: ReturnType<typeof useSync> }) {
  const qc = useQueryClient();
  const img = useImg();
  const [shown, setShown] = useState<Set<number>>(new Set());  // ПВЗ, где показываем скрытые
  // rid с точками (eAe.dc18….0.0) — в URL кодируем; «Скрыть» строки = все её позиции
  const toggle = useMutation({
    mutationFn: (os: Order[]) => Promise.all(os.map((o) => api(`/accounts/${a.id}/orders/${encodeURIComponent(o.rid)}`,
      { method: "PATCH", body: { hidden: !o.hidden } }))),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });
  if (!a.orders.length) return <div className="card empty">Активных заказов нет</div>;
  return (
    <div className="pvz-list">
      {byPvz(a.orders).map((g) => {
        const vis = visible(g.orders);
        const hidden = g.orders.length - vis.length;
        const ready = vis.filter((o) => o.group_id === 5).length;
        const going = vis.filter((o) => o.group_id === 3).length;
        const rows = lines(shown.has(g.id) ? g.orders : vis);
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
              {ready > 0 && <PickupButton g={g} a={a} sync={sync} />}
            </div>
          </div>
        );
      })}
      {sync.err && <p className="err">{sync.err}</p>}
    </div>
  );
}

// ---------- История заказов ----------
const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const daysAgo = (n: number) => iso(new Date(Date.now() - n * 86400_000));

function HistoryTab({ a }: { a: AccountFull }) {
  const img = useImg();
  const [from, setFrom] = useState(() => daysAgo(6));  // по умолчанию — 7 дней, включая сегодня
  const [to, setTo] = useState(() => iso(new Date()));
  const q = useQuery({ queryKey: ["accounts", a.id, "purchases", from, to],
    queryFn: () => api<Purchase[]>(`/accounts/${a.id}/purchases?${new URLSearchParams({ ...(from && { from }), ...(to && { to }) })}`) });
  const preset = (days: number | null) => { setFrom(days == null ? "" : daysAgo(days - 1)); setTo(days == null ? "" : iso(new Date())); };
  const active = !from && !to ? "all" : to === iso(new Date()) && from === daysAgo(6) ? 7 : to === iso(new Date()) && from === daysAgo(29) ? 30 : "";
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
        <label className="small">С <input type="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} /></label>
        <label className="small">По <input type="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} /></label>
        <div className="seg">
          <button className={active === "all" ? "on" : ""} onClick={() => preset(null)}>Всё время</button>
          <button className={active === 7 ? "on" : ""} onClick={() => preset(7)}>7 дней</button>
          <button className={active === 30 ? "on" : ""} onClick={() => preset(30)}>30 дней</button>
        </div>
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
  return (
    <div className="card">
      <div className="filters">
        <span className="muted small">WB отдаёт 20 последних операций — старые копятся здесь.</span>
        {sync.busy ? <span className="muted small"><span className="spin" /> Обновляю… (~30 с)</span>
          : <button className="btn ghost sm" onClick={sync.start}>↻ Обновить</button>}
      </div>
      {sync.err && <p className="err">{sync.err}</p>}
      {!(q.data ?? []).length ? <p className="empty">{q.isLoading ? "Загрузка…" : "Операций пока нет — нажмите «Обновить» (браузер ~30 с)"}</p> : (
        <div className="ops">
          {q.data!.map((op) => (
            <div key={op.tx_id} className="op">
              <span className={`op-ico ${op.op_type === 0 ? "in" : "out"}`}>{op.op_type === 0 ? "↑" : "↓"}</span>
              <div className="op-main"><div>{op.name ?? "—"}</div><div className="muted small">{when(op.dt)}</div></div>
              <Note a={a} op={op} />
              <b className={`num op-sum ${(op.amount ?? 0) > 0 ? "ok-t" : ""}`}>{signed(op.amount)}</b>
            </div>
          ))}
        </div>
      )}
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

const TABS = [["orders", "Заказы"], ["history", "История заказов"], ["balance", "Баланс"], ["vb", "ВБ-баллы"]] as const;

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
  const counts = { orders: visible(a.orders).length, history: a.counts.purchases, balance: a.counts.balance, vb: a.counts.vb };
  return (
    <section>
      {back}
      <div className="acc-top">
        <div className="acc-title">
          <StatusDot a={a} /><Title a={a} />
          {a.wallet_pct != null && <span className="badge in">Кошелёк −{a.wallet_pct}%</span>}
          <span className={`pill ${a.status === "active" ? "ok" : "bad"}`}>{STATUS[a.status] ?? a.status}</span>
          {a.status === "relogin" && <button className="btn sm" onClick={() => setRelogin({ a, n: Date.now() })}>Войти заново</button>}
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
      {a.proxy_down && <div className="small up-t">прокси не ответил {a.proxy_fails} раз подряд, последний {time(a.proxy_fail_at)}</div>}
      <div className="tiles acc-tiles">
        <div className="tile"><b>{rub(a.funds)}</b><span>Кошелёк WB</span></div>
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
  const sync = useSync(a);  // один на страницу: «Готовы забрать» и «Обновить» баланса — один и тот же визит
  return tab === "history" ? <HistoryTab a={a} /> : tab === "balance" ? <BalanceTab a={a} sync={sync} />
    : tab === "vb" ? <VbTab a={a} /> : <OrdersTab a={a} sync={sync} />;
}
