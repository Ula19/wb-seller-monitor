import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, time, type Account, type LoginRequest, type Order, type Proxy } from "../api";

const GROUP: Record<number, string> = { 1: "оформлен", 2: "сборка", 3: "в пути", 5: "в ПВЗ" };
const ACTIVE = ["new", "running", "wait_code"];

// сброс месячного лимита — 1-е число следующего месяца
const resetDate = () => {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth() + 1, 1).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
};

function Connect({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [phone, setPhone] = useState("");
  const [kind, setKind] = useState<"retail" | "business">("retail");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [sent, setSent] = useState<number | null>(null);  // заявка, по которой код уже отправлен
  const proxies = useQuery({ queryKey: ["proxies"], queryFn: () => api<Proxy[]>("/proxies") });
  const good = (proxies.data ?? []).filter((p) => p.ok);
  const [proxyId, setProxyId] = useState<string | null>(null);  // null — ещё не выбирали: первый проверенный
  const pid = proxyId ?? String(good[0]?.id ?? "");
  const chosen = good.find((p) => String(p.id) === pid);
  // статус заявки: worker берёт её раз в 2 с, открывает WB в браузере, ждёт код
  const req = useQuery({
    queryKey: ["login"], queryFn: () => api<LoginRequest>("/accounts/login"),
    refetchInterval: (q) => (q.state.data && ACTIVE.includes(q.state.data.status) ? 2000 : false),
  });
  const r = req.data;
  const active = !!r && ACTIVE.includes(r.status);
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "ошибка");
  const start = useMutation({
    mutationFn: () => api("/accounts/login", { method: "POST", body: { phone, kind, proxy_id: pid ? Number(pid) : null } }),
    onSuccess: () => { setError(""); qc.invalidateQueries({ queryKey: ["login"] }); },
    onError: fail,
  });
  const send = useMutation({
    mutationFn: () => api("/accounts/login/code", { method: "POST", body: { code } }),
    onSuccess: () => { setCode(""); setError(""); setSent(r?.id ?? null); qc.invalidateQueries({ queryKey: ["login"] }); },
    onError: fail,
  });
  const reset = () => { setPhone(""); setCode(""); setError(""); setSent(null); qc.invalidateQueries({ queryKey: ["login"] }); };
  // 404 — вход уже закончился сам: просто обновить статус
  const cancel = useMutation({ mutationFn: () => api("/accounts/login", { method: "DELETE" }), onSettled: reset });
  const resend = useMutation({
    mutationFn: (via: "push" | "sms") => api("/accounts/login/resend", { method: "POST", body: { via } }),
    onSuccess: () => { setError(""); qc.invalidateQueries({ queryKey: ["login"] }); },
    onError: fail,
  });
  const [now, setNow] = useState(Date.now());
  useEffect(() => {  // таймер «Отправить ещё раз через 0:42» — раз в секунду, пока ждём код
    if (r?.status !== "wait_code") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [r?.status]);
  const left = r?.resend_after ? Math.ceil((new Date(r.resend_after).getTime() - now) / 1000) : 0;
  const cancelBtn = <button type="button" className="link danger" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Отменить</button>;
  useEffect(() => {  // подключён → обновить список и очистить форму
    if (r?.status === "done") { onDone(); setPhone(""); }
  }, [r?.id, r?.status]);

  return (
    <div className="card connect">
      <h2>Подключить аккаунт WB</h2>
      {!active ? (
        <form className="add" onSubmit={(e) => { e.preventDefault(); start.mutate(); }}>
          <div className="phone"><span>+7</span><input inputMode="tel" placeholder="900 123-45-67" value={phone} onChange={(e) => setPhone(e.target.value)} /></div>
          <select value={kind} onChange={(e) => setKind(e.target.value as "retail" | "business")}>
            <option value="retail">Обычный (розница)</option>
            <option value="business">Бизнес</option>
          </select>
          <select value={pid} onChange={(e) => setProxyId(e.target.value)}>
            {good.map((p) => <option key={p.id} value={p.id}>{p.label} · {p.exit_ip}{p.used_by.length ? ` · аккаунтов: ${p.used_by.length}` : ""}</option>)}
            <option value="">Без прокси</option>
          </select>
          <button className="btn" disabled={start.isPending || phone.replace(/\D/g, "").length < 10}>Получить код</button>
          {!chosen && <p className="full up-t small">Без прокси вход пойдёт через IP мониторинга. Свой прокси — в разделе «Прокси».</p>}
        </form>
      ) : r.status === "wait_code" && sent !== r.id ? (
        <form className="add" onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
          <p className="full">{r.sent_via === "SMS" ? "Код отправлен по СМС." : r.sent_via === "PUSH" ? "Код отправлен: в приложение WB."
            : "WB отправил код — пуш в приложение Wildberries или SMS."} Введите его:</p>
          <input inputMode="numeric" autoFocus placeholder="Код" value={code} onChange={(e) => setCode(e.target.value)} />
          <button className="btn" disabled={send.isPending || code.length < 4}>Войти</button>
          <p className="full small resend">
            {r.resend || resend.isPending ? <span className="muted">Запрашиваю новый код…</span>
              : left > 0 ? <span className="muted">Отправить ещё раз через {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</span>
              : <>
                <button type="button" className="link" onClick={() => resend.mutate(r.sent_via === "SMS" ? "sms" : "push")}>Отправить ещё раз</button>
                {r.sent_via !== "SMS" && <> · <button type="button" className="link" onClick={() => resend.mutate("sms")}>Прислать по СМС</button></>}
              </>}
            {" · "}{cancelBtn}
          </p>
        </form>
      ) : (
        <p><span className="spin" /> {r.status === "wait_code" || sent === r.id ? "Проверяю код…" : r.status === "new" ? "Запускаю вход… (если идёт другой вход — подождёт его)" : "Открываю WB и ввожу номер… (до 1–2 минут)"}
          {" "}{cancelBtn}</p>
      )}
      {r?.status === "error" && !start.isPending && <p className="err">Не вышло: {r.error}</p>}
      {r?.status === "done" && <p className="ok-t">Аккаунт подключён ✓</p>}
      {error && <p className="err">{error}</p>}
      <p className="muted small">Номер нужен только на время входа и стирается после него. Сессия хранится зашифрованной.</p>
    </div>
  );
}

const ddmm = (iso: string) => new Date(iso).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
const visible = (orders: Order[]) => orders.filter((o) => !o.hidden);

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

function Title({ a }: { a: Account }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api(`/accounts/${a.id}`, { method: "PATCH", body: { label: edit } }),
    onSuccess: () => { setEdit(null); qc.invalidateQueries({ queryKey: ["accounts"] }); },
  });
  if (edit !== null) return (
    <form className="tgt-edit" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <input autoFocus maxLength={64} placeholder={a.name ?? "Имя"} value={edit} onChange={(e) => setEdit(e.target.value)} />
      <button className="btn sm" disabled={save.isPending}>OK</button>
      <button type="button" className="link" onClick={() => setEdit(null)}>✕</button>
    </form>
  );
  return (
    <span>
      <b>{a.label ?? a.name ?? "Без имени"}</b>{" "}
      <button className="mini" title="Своё имя аккаунта" onClick={() => setEdit(a.label ?? "")}>✎</button>
      {a.name && a.name !== a.label && <span className="muted small"> {a.label ? `в WB: ${a.name}` : "(имя из WB)"}</span>}
    </span>
  );
}

function Card({ a }: { a: Account }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const del = useMutation({
    mutationFn: () => api(`/accounts/${a.id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });
  const limitNote = a.limit_left === 0 ? <div className="bad-t small">лимит исчерпан — Кошелёк без скидки</div>
    : a.monthly_limit && a.limit_left != null && a.limit_left < a.monthly_limit * 0.1
      ? <div className="up-t small">лимит почти исчерпан</div> : null;
  const transit = visible(a.orders).filter((o) => o.group_id === 3).length;
  const pvz = visible(a.orders).filter((o) => o.group_id === 5).length;
  return (
    <div className="card acc">
      <div className="acc-head">
        <div>
          <Title a={a} /> <span className="muted nowrap">· {a.phone ?? a.phone_mask}</span>
          <div className="muted small">{a.kind === "business" ? "Бизнес" : "Обычный"} · обновлён {time(a.state_at)}</div>
          <div className="small">{a.proxy ? <span className="muted">прокси: {a.proxy.label} · {a.proxy.exit_ip ?? a.proxy.server}</span>
            : <span className="up-t">без своего прокси — IP мониторинга</span>}</div>
          {a.proxy_down && <div className="small up-t">прокси не ответил {a.proxy_fails} раз подряд, последний {time(a.proxy_fail_at)}</div>}
        </div>
        <span className={`pill ${a.status === "active" ? "ok" : "bad"}`}>
          {a.status === "active" ? "активен" : a.status === "relogin" ? "нужен вход" : "выключен"}
        </span>
      </div>
      <div className="kv">
        <div><span>Кошелёк</span><b>{a.wallet_pct != null ? `−${a.wallet_pct}%` : "—"}</b></div>
        <div><span>Кошелёк WB</span><b>{rub(a.funds)}</b></div>
        <div><span>ВБ-баллы</span><b>{a.vb_points != null ? a.vb_points.toLocaleString("ru-RU") : "—"}</b></div>
        <div><span>В пути / в ПВЗ</span><b>{transit} / {pvz}</b></div>
        <div><span>Процент выкупа</span><b>{a.purchase_pct != null ? `${a.purchase_pct}%` : "—"}</b></div>
      </div>
      {a.monthly_limit ? (
        <div className="limit">
          <div className="bar"><i style={{ width: `${Math.max(0, Math.min(100, ((a.limit_left ?? 0) / a.monthly_limit) * 100))}%` }} /></div>
          <div className="small">
            Остаток лимита скидки <b>{rub(a.limit_left)}</b> из {rub(a.monthly_limit)} · сброс {resetDate()}
            <div className="muted">заказ до {rub(a.max_order)}, позиция до {rub(a.max_rid)}</div>
            {limitNote}
          </div>
        </div>
      ) : a.limit_left != null && <div className="small">Остаток лимита скидки: <b>{rub(a.limit_left)}</b>{limitNote}</div>}
      <div className="acc-foot">
        <button className="link" onClick={() => setOpen(!open)}>Заказы ({visible(a.orders).length}) {open ? "▴" : "▾"}</button>
        <button className="link danger" onClick={() => confirm(
          `Удалить аккаунт ${[a.label ?? a.name, a.phone ?? a.phone_mask].filter(Boolean).join(" ")}?\n\n`
          + "Сессия удалится, бизнес-цены и заказы под ним перестанут обновляться. Вход — заново по коду.") && del.mutate()}>Удалить</button>
      </div>
      {open && <Orders accountId={a.id} orders={a.orders} />}
    </div>
  );
}

export default function Accounts() {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/accounts"), refetchInterval: 60_000 });
  const list = accounts.data ?? [];
  const sum = (f: (a: Account) => number) => list.reduce((s, a) => s + f(a), 0);
  return (
    <section>
      <h1>Аккаунты</h1>
      <div className="tiles">
        <div className="tile"><b>{rub(sum((a) => a.funds ?? 0))}</b><span>Кошелёк WB</span></div>
        <div className="tile"><b>{sum((a) => a.vb_points ?? 0).toLocaleString("ru-RU")}</b><span>ВБ-баллы</span></div>
        <div className="tile"><b>{sum((a) => visible(a.orders).filter((o) => o.group_id === 3).length)}</b><span>в пути</span></div>
        <div className="tile"><b>{sum((a) => visible(a.orders).filter((o) => o.group_id === 5).length)}</b><span>в ПВЗ</span></div>
        <div className="tile"><b className={list.some((a) => a.status !== "active") ? "up-t" : ""}>{list.filter((a) => a.status !== "active").length}</b><span>требуют входа</span></div>
      </div>
      <div className="acc-grid">
        {list.map((a) => <Card key={a.id} a={a} />)}
        <Connect onDone={() => qc.invalidateQueries({ queryKey: ["accounts"] })} />
      </div>
    </section>
  );
}
