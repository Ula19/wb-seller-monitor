import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, time, type Account, type LoginRequest, type Order } from "../api";

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
  // статус заявки: worker берёт её раз в 2 с, открывает WB в браузере, ждёт код
  const req = useQuery({
    queryKey: ["login"], queryFn: () => api<LoginRequest>("/accounts/login"),
    refetchInterval: (q) => (q.state.data && ACTIVE.includes(q.state.data.status) ? 2000 : false),
  });
  const r = req.data;
  const active = !!r && ACTIVE.includes(r.status);
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "ошибка");
  const start = useMutation({
    mutationFn: () => api("/accounts/login", { method: "POST", body: { phone, kind } }),
    onSuccess: () => { setError(""); qc.invalidateQueries({ queryKey: ["login"] }); },
    onError: fail,
  });
  const send = useMutation({
    mutationFn: () => api("/accounts/login/code", { method: "POST", body: { code } }),
    onSuccess: () => { setCode(""); setError(""); setSent(r?.id ?? null); qc.invalidateQueries({ queryKey: ["login"] }); },
    onError: fail,
  });
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
          <button className="btn" disabled={start.isPending || phone.replace(/\D/g, "").length < 10}>Получить код</button>
        </form>
      ) : r.status === "wait_code" && sent !== r.id ? (
        <form className="add" onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
          <p className="full">WB отправил код — пуш в приложение Wildberries или SMS. Введите его:</p>
          <input inputMode="numeric" autoFocus placeholder="Код" value={code} onChange={(e) => setCode(e.target.value)} />
          <button className="btn" disabled={send.isPending || code.length < 4}>Войти</button>
        </form>
      ) : (
        <p><span className="spin" /> {r.status === "wait_code" ? "Проверяю код…" : r.status === "new" ? "Запускаю вход… (если идёт другой вход — подождёт его)" : "Открываю WB и ввожу номер… (до 1–2 минут)"}</p>
      )}
      {r?.status === "error" && !start.isPending && <p className="err">Не вышло: {r.error}</p>}
      {r?.status === "done" && <p className="ok-t">Аккаунт подключён ✓</p>}
      {error && <p className="err">{error}</p>}
      <p className="muted small">Номер нужен только на время входа и стирается после него. Сессия хранится зашифрованной.</p>
    </div>
  );
}

function Orders({ orders }: { orders: Order[] }) {
  if (!orders.length) return <p className="muted small">Активных заказов нет.</p>;
  return (
    <ul className="orders">
      {orders.map((o) => (
        <li key={o.rid}>
          <a href={`https://www.wildberries.ru/catalog/${o.nm_id}/detail.aspx`} target="_blank" rel="noreferrer">{o.name ?? `арт. ${o.nm_id}`}</a>
          <span className="num">{rub(o.price)}</span>
          <span className={`pill g${o.group_id ?? 0}`}>{o.status_name ?? GROUP[o.group_id ?? 0] ?? "—"}</span>
          {o.expiry_at && <span className="muted small">заберите до {new Date(o.expiry_at).toLocaleDateString("ru-RU")}</span>}
        </li>
      ))}
    </ul>
  );
}

function Card({ a }: { a: Account }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const del = useMutation({
    mutationFn: () => api(`/accounts/${a.id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });
  const low = a.monthly_limit && a.limit_left != null && a.limit_left < a.monthly_limit * 0.1;
  const transit = a.orders.filter((o) => o.group_id === 3).length;
  const pvz = a.orders.filter((o) => o.group_id === 5).length;
  return (
    <div className="card acc">
      <div className="acc-head">
        <div>
          <b>{a.phone_mask}</b>
          <div className="muted small">{a.kind === "business" ? "Бизнес" : "Обычный"} · обновлён {time(a.state_at)}</div>
        </div>
        <span className={`pill ${a.status === "active" ? (low ? "warn" : "ok") : "bad"}`}>
          {a.status === "active" ? (low ? "лимит почти исчерпан" : "активен") : a.status === "relogin" ? "нужен вход" : "выключен"}
        </span>
      </div>
      <div className="kv">
        <div><span>Кошелёк</span><b>{a.wallet_pct != null ? `−${a.wallet_pct}%` : "—"}</b></div>
        <div><span>Баланс</span><b>{rub(a.wallet_balance)}</b></div>
        <div><span>Бонусы</span><b>{rub(a.bonus_balance)}</b></div>
        <div><span>В пути / в ПВЗ</span><b>{transit} / {pvz}</b></div>
      </div>
      {a.monthly_limit ? (
        <div className="limit">
          <div className="bar"><i style={{ width: `${Math.max(0, Math.min(100, ((a.limit_left ?? 0) / a.monthly_limit) * 100))}%` }} /></div>
          <div className="small">
            Остаток лимита скидки <b>{rub(a.limit_left)}</b> из {rub(a.monthly_limit)} · сброс {resetDate()}
            <div className="muted">заказ до {rub(a.max_order)}, позиция до {rub(a.max_rid)}</div>
          </div>
        </div>
      ) : a.limit_left != null && <p className="small">Остаток лимита скидки: <b>{rub(a.limit_left)}</b></p>}
      <div className="acc-foot">
        <button className="link" onClick={() => setOpen(!open)}>Заказы ({a.orders.length}) {open ? "▴" : "▾"}</button>
        <button className="link danger" onClick={() => confirm(`Удалить аккаунт ${a.phone_mask}? Сессия будет стёрта.`) && del.mutate()}>Удалить</button>
      </div>
      {open && <Orders orders={a.orders} />}
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
        <div className="tile"><b>{rub(sum((a) => a.wallet_balance ?? 0))}</b><span>в Кошельках</span></div>
        <div className="tile"><b>{rub(sum((a) => a.bonus_balance ?? 0))}</b><span>бонусы</span></div>
        <div className="tile"><b>{sum((a) => a.orders.filter((o) => o.group_id === 3).length)}</b><span>в пути</span></div>
        <div className="tile"><b>{sum((a) => a.orders.filter((o) => o.group_id === 5).length)}</b><span>в ПВЗ</span></div>
        <div className="tile"><b className={list.some((a) => a.status !== "active") ? "up-t" : ""}>{list.filter((a) => a.status !== "active").length}</b><span>требуют входа</span></div>
      </div>
      <div className="acc-grid">
        {list.map((a) => <Card key={a.id} a={a} />)}
        <Connect onDone={() => qc.invalidateQueries({ queryKey: ["accounts"] })} />
      </div>
    </section>
  );
}
