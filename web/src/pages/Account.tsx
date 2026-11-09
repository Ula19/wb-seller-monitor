// Страница аккаунта WB (/accounts/:id): шапка (имя, статус, обновление, прокси), плитки денег и лимита, заказы.
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, time, type AccountFull, type Order } from "../api";
import { ddmm, GROUP, Limit, points, STATUS, StatusDot, Title, visible } from "../accounts";
import LoginModal, { type Relogin } from "./LoginModal";

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

function Orders({ accountId, orders }: { accountId: number; orders: Order[] }) {
  const qc = useQueryClient();
  const [showHidden, setShowHidden] = useState(false);
  // rid с точками (eAe.dc18….0.0) — в URL кодируем
  const toggle = useMutation({
    mutationFn: (o: Order) => api(`/accounts/${accountId}/orders/${encodeURIComponent(o.rid)}`,
      { method: "PATCH", body: { hidden: !o.hidden } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });
  const hidden = orders.filter((o) => o.hidden);
  const rows = showHidden ? orders : visible(orders);
  const row = (o: Order) => (
    <li key={o.rid} className={o.hidden ? "off" : undefined}>
      <a href={`https://www.wildberries.ru/catalog/${o.nm_id}/detail.aspx`} target="_blank" rel="noreferrer">{o.name ?? `арт. ${o.nm_id}`}</a>
      <span className="num">{rub(o.price)}</span>
      <span className={`pill g${o.group_id ?? 0}`}>{o.status_name ?? GROUP[o.group_id ?? 0] ?? "—"}</span>
      {o.paid === false && <span className="badge out">не оплачен</span>}
      {o.paid === true && <span className="muted small">оплачен</span>}
      {o.expiry_at ? <span className="muted small">заберите до {ddmm(o.expiry_at)}</span>
        : o.delivery_at && <span className="muted small">ожидается до {ddmm(o.delivery_at)}</span>}
      <button className="link small" disabled={toggle.isPending} onClick={() => toggle.mutate(o)}>{o.hidden ? "Вернуть" : "Скрыть"}</button>
      {o.dst_office_id != null && <span className="pvz muted small">
        ПВЗ: {o.office_address ? <>{o.office_address}{o.office_hours && ` · ${o.office_hours}`}</> : `№${o.dst_office_id}`}
      </span>}
    </li>
  );
  return (
    <>
      {rows.length ? <ul className="orders">{rows.map(row)}</ul> : <p className="muted small">Активных заказов нет.</p>}
      {hidden.length > 0 && <button className="link small" onClick={() => setShowHidden(!showHidden)}>
        {showHidden ? "Убрать скрытые" : `Показать скрытые (${hidden.length})`}</button>}
    </>
  );
}

export default function Account() {
  const id = Number(useParams().id);
  const nav = useNavigate();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["accounts", id], queryFn: () => api<AccountFull>(`/accounts/${id}`), retry: false,
    refetchInterval: (q) => (q.state.data?.polling || q.state.data?.browser_sync ? 2000 : 60_000) });
  const a = q.data;
  const { poll, note } = useRefresh(a);
  const [relogin, setRelogin] = useState<Relogin | null>(null);
  const del = useMutation({
    mutationFn: () => api(`/accounts/${id}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["accounts"] }); nav("/accounts"); },
  });
  const back = <Link to="/accounts" className="back">← Все аккаунты</Link>;
  if (q.isError) return <section>{back}<div className="card empty">Аккаунт не найден</div></section>;
  if (!a) return <section>{back}<p className="muted">Загрузка…</p></section>;
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
      <div className="card"><h2>Заказы</h2><Orders accountId={a.id} orders={a.orders} /></div>
      <LoginModal open={!!relogin} onClose={() => setRelogin(null)} relogin={relogin} />
    </section>
  );
}
