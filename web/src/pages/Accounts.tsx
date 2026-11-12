// «Аккаунты»: поиск, список или плитки, итоги, «Обновить все», вход новым аккаунтом в модалке.
// Клик по аккаунту — его страница (/accounts/:id) с заказами, историей, балансом и ВБ-баллами.
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, time, type Account } from "../api";
import { Blocked, inPvz, Limit, points, StatusDot, transit, unpaid, visible } from "../accounts";
import { useSort } from "../sort";
import CertHistory from "./CertHistory";
import CertModal from "./CertModal";
import LoginModal from "./LoginModal";
import PayModal from "./PayModal";

const VIEW_KEY = "accounts.view";  // «Список | Плитки» — помним в браузере
const readView = (): "list" | "tiles" => {
  try { return localStorage.getItem(VIEW_KEY) === "tiles" ? "tiles" : "list"; } catch { return "list"; }
};

export const useAccounts = () => useQuery({ queryKey: ["accounts"], queryFn: () => api<Account[]>("/accounts"),
  refetchInterval: (q) => (q.state.data?.some((a) => a.polling) ? 2000 : 60_000) });

// поиск по имени (своему и из WB) и по номеру — цифры сравниваем без пробелов и скобок
const matches = (a: Account, q: string) => {
  const s = q.trim().toLowerCase();
  if (!s) return true;
  const digits = s.replace(/\D/g, "");
  return [a.title, a.name, a.label].some((t) => t?.toLowerCase().includes(s))
    || (digits.length >= 2 && (a.phone ?? a.phone_mask).replace(/\D/g, "").includes(digits));
};

function OrdersNote({ a }: { a: Account }) {
  const t = transit(a), p = inPvz(a);
  if (!t && !p) return <span className="muted">нет заказов</span>;
  return <>{t > 0 && `${t} в пути`}{t > 0 && p > 0 && " · "}{p > 0 && <b className="ok-t">{p} в ПВЗ</b>}</>;
}

function Unpaid({ a }: { a: Account }) {
  const u = unpaid(a);
  return u ? <span className="up-t">{rub(u)}</span> : <span className="muted">—</span>;
}

function Name({ a }: { a: Account }) {
  return (
    <span className="acc-name">
      <StatusDot a={a} /><Link to={`/accounts/${a.id}`} onClick={(e) => e.stopPropagation()}><b>{a.title}</b></Link>
      <span className="muted small"> {a.phone ?? a.phone_mask}</span> <Blocked a={a} />
    </span>
  );
}

const plural = (n: number, one: string, few: string, many: string) =>
  n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many;
type Board = { id: number; address: string; go: number; ready: number; accs: { a: Account; go: number; ready: number }[] };

/** Все ПВЗ со всех аккаунтов (видимые заказы): в пути / выдача / сколько аккаунтов; клик — кто там, под строкой. */
export function pvzBoard(all: Account[]): Board[] {
  const m = new Map<number, Board>();
  for (const a of all) for (const o of visible(a.orders)) {
    if (o.group_id !== 3 && o.group_id !== 5) continue;
    const id = o.dst_office_id ?? 0;
    const b = m.get(id) ?? { id, address: o.office_address ?? (id ? `ПВЗ №${id}` : "ПВЗ не указан"), go: 0, ready: 0, accs: [] };
    const x = b.accs.find((x) => x.a.id === a.id) ?? (b.accs.push({ a, go: 0, ready: 0 }), b.accs[b.accs.length - 1]);
    const k = o.group_id === 5 ? "ready" : "go";
    b[k] += 1; x[k] += 1;
    m.set(id, b);
  }
  const order = (x: { go: number; ready: number }, y: { go: number; ready: number }) => y.ready - x.ready || y.go - x.go;
  return [...m.values()].map((b) => ({ ...b, accs: b.accs.sort(order) })).sort(order);
}

/** Шестая плитка «пунктов выдачи»: клик — панель под рядом плиток (поверх таблицы), внутри все ПВЗ; клик по ПВЗ — кто там. */
function PvzTile({ all }: { all: Account[] }) {
  const [shown, setShown] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!shown) return;
    const click = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setShown(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setShown(false); };
    document.addEventListener("mousedown", click);
    window.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", click); window.removeEventListener("keydown", esc); };
  }, [shown]);
  const rows = pvzBoard(all);
  const counts = (x: { go: number; ready: number }) => <>
    {x.go > 0 && <span>в пути {x.go}</span>}{x.ready > 0 && <span className="ok-t">выдача {x.ready}</span>}</>;
  // ponytail: display: contents — плитка остаётся в сетке, а клик «вне» считается по плитке и панели вместе
  return (
    <div className="pvz-tile" ref={ref}>
      <button className="tile" aria-expanded={shown} onClick={() => rows.length && setShown(!shown)}>
        <b>{rows.length}</b><span>пунктов выдачи</span>
      </button>
      {shown && (
        <div className="card pvz-board">
          {rows.map((b) => (
            <div key={b.id} className="pvz-line">
              <button className="pvz-row" aria-expanded={open === b.id} onClick={() => setOpen(open === b.id ? null : b.id)}>
                <b>{b.address}</b>{counts(b)}<span className="muted">{b.accs.length} {plural(b.accs.length, "аккаунт", "аккаунта", "аккаунтов")}</span>
              </button>
              {open === b.id && (
                <div className="pvz-pop small">
                  {b.accs.map((x, i) => <span key={x.a.id}>{i > 0 && " · "}<Link to={`/accounts/${x.a.id}`}>{x.a.title}</Link> —{" "}
                    {[x.ready && `выдача ${x.ready}`, x.go && `в пути ${x.go}`].filter(Boolean).join(", ")}</span>)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

type Fn = "login" | "cert" | "history" | "pay";

/** «Функции»: всё, что делается над аккаунтами, — одним меню по группам. Закрытие — клик вне или Esc. */
function Functions({ on }: { on: (f: Fn) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const click = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", click);
    window.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", click); window.removeEventListener("keydown", esc); };
  }, [open]);
  const item = (f: Fn, t: string, d: string) => (
    <button role="menuitem" className="fn-item" onClick={() => { setOpen(false); on(f); }}><b>{t}</b><span className="muted small">{d}</span></button>);
  return (
    <div className="fn" ref={ref}>
      <button className="btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>Функции ▾</button>
      {open && (
        <div className="fn-menu card" role="menu">
          <div className="fn-group">Аккаунты</div>
          {item("login", "Добавить аккаунт", "Подключить аккаунт WB по номеру и коду")}
          <div className="fn-group">Сертификаты</div>
          {item("cert", "Добавить сертификат", "Пополнить кошельки кодами сертификатов")}
          {item("history", "История сертификатов", "Все добавленные коды: когда, куда, номинал")}
          <div className="fn-group">Заказы</div>
          {item("pay", "Оплата товаров", "Оплатить неоплаченные заказы с кошелька")}
        </div>
      )}
    </div>
  );
}

type Th = ReturnType<typeof useSort<Account>>["th"];

function ListView({ list, th, total }: { list: Account[]; th: Th; total: number }) {
  const nav = useNavigate();
  return (
    <div className="card table-wrap">
      <table className="acc-table">
        <thead><tr><th className="no">№</th>
          {th("title", `Аккаунт · ${list.length === total ? total : `${list.length} из ${total}`}`, "")}
          {th("funds", "Кошелёк WB")}{th("vb", "ВБ-баллы")}{th("limit", "Лимит скидки", "")}{th("orders", "Заказы", "")}
          {th("unpaid", "Не оплачено")}<th className="upd">Обновлён</th></tr></thead>
        <tbody>
          {list.map((a, i) => (
            <tr key={a.id} className="click" onClick={() => nav(`/accounts/${a.id}`)}>
              <td className="no muted">{i + 1}</td>
              <td><Name a={a} /></td>
              <td className="num money">{rub(a.funds)}</td>
              <td className="num">{points(a.vb_points)}</td>
              <td className="lim"><Limit a={a} short /></td>
              <td><OrdersNote a={a} /></td>
              <td className="num"><Unpaid a={a} /></td>
              <td className="upd muted">{a.polling ? <span className="spin" /> : time(a.state_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TilesView({ list }: { list: Account[] }) {
  const nav = useNavigate();
  return (
    <div className="acc-grid">
      {list.map((a, i) => (
        <div key={a.id} className="card acc click" onClick={() => nav(`/accounts/${a.id}`)}>
          <div className="acc-head"><span><span className="no muted small">{i + 1}</span> <Name a={a} /></span><span className="muted small">{a.polling ? <span className="spin" /> : time(a.state_at)}</span></div>
          <div className="kv">
            <div><span>Кошелёк WB</span><b className="money">{rub(a.funds)}</b></div>
            <div><span>ВБ-баллы</span><b>{points(a.vb_points)}</b></div>
          </div>
          <div className="small"><span className="muted">Лимит скидки</span><Limit a={a} short /></div>
          <div className="small"><OrdersNote a={a} /></div>
          {unpaid(a) > 0 && <div className="small"><span className="muted">Не оплачено </span><Unpaid a={a} /></div>}
        </div>
      ))}
    </div>
  );
}

export default function Accounts() {
  const qc = useQueryClient();
  const accounts = useAccounts();
  const all = accounts.data ?? [];
  const [q, setQ] = useState("");
  const [view, setView] = useState(readView);
  const [fn, setFn] = useState<Fn | null>(null);
  const close = () => setFn(null);
  const [note, setNote] = useState("");
  const pollAll = useMutation({
    mutationFn: () => api<{ count: number }>("/accounts/poll-all", { method: "POST" }),
    onSuccess: (r) => { setNote(r.count ? "" : "все только что обновлены"); qc.invalidateQueries({ queryKey: ["accounts"] }); },
    onError: (e: unknown) => setNote(e instanceof ApiError ? e.message : "ошибка"),
  });
  const pick = (v: "list" | "tiles") => { setView(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* приватный режим */ } };
  // порядок — один для списка и плиток; по умолчанию — как пришёл с сервера
  const { sorted: list, th } = useSort(all.filter((a) => matches(a, q)), {
    title: (a) => a.title, funds: (a) => a.funds, vb: (a) => a.vb_points, limit: (a) => a.limit_left,
    orders: (a) => inPvz(a) * 1000 + transit(a), unpaid: (a) => unpaid(a) || null,  // 0 — как пустое, внизу
  }, "accounts");
  const sum = (f: (a: Account) => number) => all.reduce((s, a) => s + f(a), 0);
  const needLogin = all.filter((a) => a.status !== "active").length;
  return (
    <section>
      <div className="page-head">
        <h1>Аккаунты</h1>
        <input className="search" placeholder="Поиск по имени и номеру" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="seg">
          <button className={view === "list" ? "on" : ""} onClick={() => pick("list")}>Список</button>
          <button className={view === "tiles" ? "on" : ""} onClick={() => pick("tiles")}>Плитки</button>
        </div>
        <button className="btn ghost" disabled={pollAll.isPending || all.some((a) => a.polling)} onClick={() => pollAll.mutate()}>
          {all.some((a) => a.polling) ? <><span className="spin" /> Обновляю…</> : "↻ Обновить все"}</button>
        <Functions on={setFn} />
      </div>
      {note && <p className="muted small">{note}</p>}
      <div className="tiles">
        <div className="tile"><b>{rub(sum((a) => a.funds ?? 0))}</b><span>Кошелёк WB</span></div>
        <div className="tile"><b>{points(sum((a) => a.vb_points ?? 0))}</b><span>ВБ-баллы</span></div>
        <div className="tile"><b>{sum(transit)}</b><span>в пути</span></div>
        <div className="tile"><b>{sum(inPvz)}</b><span>в ПВЗ</span></div>
        <div className="tile"><b>{all.length}</b><span>аккаунтов{needLogin > 0 && <span className="bad-t"> · {needLogin} нужен вход</span>}</span></div>
        <PvzTile all={all} />
      </div>
      {accounts.isLoading ? <p className="muted">Загрузка…</p>
        : !all.length ? <div className="card empty">Аккаунтов пока нет. <button className="link" onClick={() => setFn("login")}>Добавить первый</button></div>
        : !list.length ? <div className="card empty">Ничего не нашлось</div>
        : view === "list" ? <ListView list={list} th={th} total={all.length} /> : <TilesView list={list} />}
      <LoginModal open={fn === "login"} onClose={close} relogin={null} />
      <CertModal open={fn === "cert"} onClose={close} all={all} />
      <CertHistory open={fn === "history"} onClose={close} all={all} />
      <PayModal open={fn === "pay"} onClose={close} all={all} />
    </section>
  );
}
