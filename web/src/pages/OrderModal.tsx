// «Заказать товар»: мастер Аккаунты → Товары → Пункты выдачи → Оплата, потом итог. В WB ходит worker: карточка товара
// и предпросмотр — запросы (order_quotes, опрос раз в 0,5 с), заказы и отмены — его очередь (autobuy_orders).
// Деньги в API — копейки. ПВЗ — только номер из списка (свободный текст в WB не уходит). Заказываем только готовые
// строки, цены берёт сервер из предпросмотра; перед отправкой worker сверит цену и деньги ещё раз.
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rubk, type Account, type BuyOrder, type PickupPoint, type Preview, type PreviewAccount,
  type PreviewLine, type ProductCard, type Quote } from "../api";
import { Modal } from "../accounts";
import { StepAccounts } from "./CertModal";
import { CertAdd } from "./PayModal";

const QUOTE_POLL_MS = 500;
const QUOTE_WAIT_MS = 90_000;
const SEARCH_DELAY_MS = 300;  // поиск ПВЗ — после паузы в наборе, а не на каждую букву
const FINAL = ["ok", "error", "unknown", "cancelled"];
const STEPS = ["Аккаунты", "Товары", "Пункты выдачи", "Оплата"];
const err = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : "ошибка");
const pcs = (n: number) => `${n} шт.`;
const sum = (xs: number[]) => xs.reduce((s, x) => s + x, 0);
const key = (acc: number, nm: number) => `${acc}:${nm}`;
const pvzLabel = (p: PickupPoint) => [p.id, p.address, p.hours].filter(Boolean).join(" · ");
const merge = (a: PickupPoint[], b: PickupPoint[]) => [...a, ...b.filter((p) => !a.some((x) => x.id === p.id))];

type Qty = Record<number, Record<number, number>>;  // nm → аккаунт → шт.

/** Запрос worker'у: POST → id, опрос, пока не done | error. */
async function runQuote<T>(path: string, body: unknown): Promise<{ id: number; result: T }> {
  const { id } = await api<{ id: number }>(path, { method: "POST", body });
  for (const t0 = Date.now(); Date.now() - t0 < QUOTE_WAIT_MS;) {
    await new Promise((r) => setTimeout(r, QUOTE_POLL_MS));
    const q = await api<Quote<T>>(`/order/quote/${id}`);
    if (q.status === "done") return { id, result: q.result as T };
    if (q.status === "error") throw new Error(q.error ?? "ошибка");
  }
  throw new Error("сервер долго не отвечает — попробуйте ещё раз");
}

function useDebounced<T>(v: T, ms: number): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t); }, [v]);
  return d;
}

function Steps({ step }: { step: number }) {
  return (
    <ol className="steps">
      {STEPS.map((t, i) => (
        <li key={t} className={i + 1 === step ? "on" : i + 1 < step ? "done" : ""}><b>{i + 1 < step ? "✓" : i + 1}</b>{t}</li>
      ))}
    </ol>
  );
}

// ---------- шаг 2: товары ----------
function FindProduct({ have, onAdd }: { have: number[]; onAdd: (p: ProductCard) => void }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const find = async () => {
    setBusy(true); setMsg("");
    try {
      const { result: p } = await runQuote<ProductCard>("/order/product", { q });
      if (have.includes(p.nm)) setMsg(`товар ${p.nm} уже добавлен`);
      else if (!p.in_stock) setMsg(`«${p.name ?? p.nm}» — нет в наличии`);
      else { onAdd(p); setQ(""); }
    } catch (e) { setMsg(err(e)); } finally { setBusy(false); }
  };
  return (
    <div className="find">
      <form className="find-row" onSubmit={(e) => { e.preventDefault(); if (q.trim() && !busy) find(); }}>
        <input aria-label="Артикул или ссылка" placeholder="Артикул или ссылка на товар WB" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <button className="btn sm" disabled={!q.trim() || busy}>{busy ? <><span className="spin" /> Ищу…</> : "Найти"}</button>
      </form>
      {msg && <div className="bad-t small">{msg}</div>}
    </div>
  );
}

function ProductQty({ p, accs, mine, setN, onRemove }: {
  p: ProductCard; accs: Account[]; mine: Record<number, number>; setN: (acc: number, n: number) => void; onRemove: () => void;
}) {
  const given = sum(accs.map((a) => mine[a.id] ?? 0));
  return (
    <div className="card sm prod">
      <div className="prod-head"><b>{p.name ?? `Товар ${p.nm}`}</b>
        <button className="x" aria-label="Убрать товар" onClick={onRemove}>✕</button></div>
      <div className="small muted">{[p.color, `арт. ${p.nm}`, `≈ ${rubk(p.price)} за шт.`].filter(Boolean).join(" · ")}
        {" "}(точная цена — на шаге «Оплата»: зависит от пункта выдачи)</div>
      <div className="small">Осталось у продавца {pcs(p.stock)} · распределено {pcs(given)}
        {given < p.stock ? <span className="muted"> · можно ещё {pcs(p.stock - given)}</span> : <span className="up-t"> · больше нет</span>}</div>
      <div className="qty-grid">
        {accs.map((a) => {
          const n = mine[a.id] ?? 0;
          const max = p.stock - (given - n);  // остаток минус отданное другим
          return (
            <label key={a.id}><span>{a.title}</span>
              <input type="number" inputMode="numeric" min={0} max={max} placeholder="0" aria-label={`Количество: ${a.title}`}
                value={n || ""} onChange={(e) => setN(a.id, Math.max(0, Math.min(max, Math.floor(Number(e.target.value) || 0))))} />
            </label>
          );
        })}
      </div>
    </div>
  );
}

// ---------- шаг 3: пункты выдачи ----------
function PvzSearch({ opts, onAdd }: { opts: PickupPoint[]; onAdd: (p: PickupPoint) => void }) {
  const [q, setQ] = useState("");
  const dq = useDebounced(q.trim(), SEARCH_DELAY_MS);
  const found = useQuery({ queryKey: ["pvz-search", dq], enabled: dq.length >= 2, staleTime: 60_000,
    queryFn: () => api<PickupPoint[]>(`/pickups/search?q=${encodeURIComponent(dq)}`) });
  return (
    <div className="pvz-search">
      <input aria-label="Поиск пункта выдачи" placeholder="Найти пункт выдачи: номер или улица и дом" value={q}
        onChange={(e) => setQ(e.target.value)} />
      {dq.length >= 2 && (found.isFetching ? <div className="small muted"><span className="spin" /> Ищу…</div>
        : found.data && !found.data.length ? <div className="small muted">Ничего не нашлось — проверьте номер или адрес</div>
        : <ul className="pvz-found">
          {(found.data ?? []).map((p) => {
            const has = opts.some((x) => x.id === p.id);
            return <li key={p.id}><button className="link" disabled={has} onClick={() => onAdd(p)}>
              {has ? "✓ " : "+ "}{pvzLabel(p)}</button></li>;
          })}
        </ul>)}
    </div>
  );
}

function StepPickups({ all, accs, prods, qty, opts, setOpts, pvzOf, setPvzOf }: {
  all: Account[]; accs: Account[]; prods: ProductCard[]; qty: Qty; opts: PickupPoint[];
  setOpts: (f: (o: PickupPoint[]) => PickupPoint[]) => void; pvzOf: Record<number, number>; setPvzOf: (p: Record<number, number>) => void;
}) {
  const own = useQuery({ queryKey: ["order-pickups", accs.map((a) => a.id).join()],
    queryFn: () => api<PickupPoint[]>(`/order/pickups?${accs.map((a) => `accounts=${a.id}`).join("&")}`) });
  useEffect(() => { if (own.data) setOpts((o) => merge(own.data, o)); }, [own.data]);
  const option = (p: PickupPoint, a?: Account) => (
    <option key={p.id} value={p.id}>{pvzLabel(p)}{a && p.accounts?.includes(a.id) ? " · здесь заказы аккаунта" : ""}</option>);
  return (
    <>
      <p className="muted">Выберите пункт выдачи для каждого аккаунта: можно один для всех или разные. Нет нужного в списке —
        найдите по номеру или адресу.</p>
      <PvzSearch opts={opts} onAdd={(p) => setOpts((o) => merge(o, [p]))} />
      <select className="pvz-all" aria-label="Один пункт выдачи для всех" value=""
        onChange={(e) => e.target.value && setPvzOf(Object.fromEntries(accs.map((a) => [a.id, Number(e.target.value)])))}>
        <option value="">Один пункт выдачи для всех аккаунтов…</option>
        {opts.map((p) => option(p))}
      </select>
      {own.isLoading && <div className="small muted"><span className="spin" /> Ищу пункты из заказов аккаунтов…</div>}
      <div className="card pvz-accs">
        {accs.map((a) => {
          const mine = prods.filter((p) => (qty[p.nm]?.[a.id] ?? 0) > 0);
          const n = sum(mine.map((p) => qty[p.nm][a.id]));
          const approx = sum(mine.map((p) => (p.price ?? 0) * qty[p.nm][a.id]));
          return (
            <div key={a.id} className="pvz-acc">
              <div><span className="muted">{all.indexOf(a) + 1}</span> <b>{a.title}</b>
                <div className="small muted">{mine.length} поз. · {pcs(n)} · ≈ {rubk(approx)}</div></div>
              <select aria-label={`Пункт выдачи: ${a.title}`} className={pvzOf[a.id] ? "" : "need"} value={pvzOf[a.id] ?? ""}
                onChange={(e) => setPvzOf({ ...pvzOf, [a.id]: Number(e.target.value) })}>
                <option value="" disabled>Выберите пункт выдачи…</option>
                {opts.map((p) => option(p, a))}
              </select>
            </div>
          );
        })}
      </div>
    </>
  );
}

// ---------- шаг 4: оплата ----------
type Row = { ln: PreviewLine; payNow: boolean; why: string | null };
type AccPlan = { a: Account; pa: PreviewAccount; rows: Row[]; lack: boolean };

type Req = { account_id: number; nm: number; qty: number; pvz_id: number };

/** Что готово к заказу и почему нет. Строки — из запроса (у аккаунта с ошибкой worker строк не присылает, а в «N из M»
 *  они нужны). Деньги и лимит постоплаты — на весь аккаунт: не хватает — не готовы все его строки этого способа оплаты
 *  (сервер и worker проверят так же). */
function plan(accs: Account[], reqs: Req[], prev: Preview, pay: Record<string, boolean>, server: Record<string, string>,
  names: Record<number, string | null>): AccPlan[] {
  return accs.map((a) => {
    const pa = prev.accounts.find((x) => x.account_id === a.id) ?? { account_id: a.id, error: "нет в предпросмотре — обновите" };
    const over = (nm: number) => prev.stock_over.find((x) => x.nm === nm);
    const mine = reqs.filter((q) => q.account_id === a.id).map((q): PreviewLine => {
      const pl = pa.lines?.find((x) => x.nm === q.nm);
      return { ...q, ...pl, name: pl?.name ?? names[q.nm] };  // пустое название из detail не затирает карточку
    });
    let rows: Row[] = mine.map((ln) => {
      const payNow = pay[key(a.id, ln.nm)] ?? !!pa.prepaid_only;
      const o = over(ln.nm);
      const why = pa.error ?? ln.error ?? (ln.total == null ? "нет цены — обновите" : null) ?? (o ? `товара не хватает: на все аккаунты нужно ${o.need}, есть ${o.stock}` : null)
        ?? server[key(a.id, ln.nm)] ?? (payNow && pa.wallet_blocked ? "Кошелёк заблокирован — оплата им недоступна" : null)
        ?? (!payNow && pa.prepaid_only ? "аккаунту доступна только предоплата" : null);
      return { ln, payNow, why };
    });
    const ready = (now: boolean) => rows.filter((r) => !r.why && r.payNow === now);
    const nowNeed = sum(ready(true).map((r) => r.ln.now ?? 0));
    const lack = nowNeed > (pa.funds ?? 0);
    if (lack) rows = rows.map((r) => (r.why || !r.payNow ? r : { ...r, why: `на Кошельке ${rubk(pa.funds)}, нужно ${rubk(nowNeed)}` }));
    const postNeed = sum(ready(false).map((r) => (r.ln.total ?? 0) * r.ln.qty));
    if (postNeed > (pa.postpaid_left ?? 0)) rows = rows.map((r) => (r.why || r.payNow ? r
      : { ...r, why: `лимит оплаты при получении: доступно ${rubk(pa.postpaid_left)}, нужно ${rubk(postNeed)}` }));
    return { a, pa, rows, lack };
  });
}

function BuyLine({ r, pa, setPay }: { r: Row; pa: PreviewAccount; setPay: (now: boolean) => void }) {
  const { ln, payNow, why } = r;
  const full = (ln.total ?? 0) * ln.qty;
  return (
    <div className={`buy-line${why ? " off" : ""}`}>
      <span className="buy-name">{ln.name ?? `Товар ${ln.nm}`} <span className="muted">{pcs(ln.qty)}</span></span>
      {ln.total != null && <span className="buy-price">
        {payNow ? <b>{rubk(ln.now)}</b> : <><b>{rubk(full)}</b>{!ln.no_discount && ln.final != null &&
          <span className="muted"> · {rubk(ln.final * ln.qty)} при оплате Кошельком</span>}</>}</span>}
      {ln.total != null && !pa.error && <span className="seg buy-mode">
        <button className={payNow ? "" : "on"} disabled={!!pa.prepaid_only} title={pa.prepaid_only ? "аккаунту доступна только предоплата" : ""}
          onClick={() => setPay(false)}>При получении</button>
        <button className={payNow ? "on" : ""} onClick={() => setPay(true)}>Оплатить сейчас</button>
      </span>}
      {ln.no_discount && <div className="bad-t small buy-why">скидки не будет: {ln.no_discount}</div>}
      <div className={`small buy-why ${why ? "muted" : "ok-t"}`}>{why ? `${why} · не заказываем` : "Готово к заказу"}</div>
    </div>
  );
}

function BuyAccount({ p, no, pvz, setPay, refresh }: {
  p: AccPlan; no: number; pvz?: PickupPoint; setPay: (k: string, v: boolean) => void; refresh: () => void;
}) {
  const { a, pa, rows, lack } = p;
  const nowSum = sum(rows.filter((r) => r.payNow).map((r) => r.ln.now ?? 0));
  const postSum = sum(rows.filter((r) => !r.payNow).map((r) => (r.ln.total ?? 0) * r.ln.qty));
  const used = pa.postpaid_limit != null && pa.postpaid_left != null ? pa.postpaid_limit - pa.postpaid_left : null;
  return (
    <div className="buy-acc">
      <div className="buy-head">
        <div><span className="muted">{no}</span> <b>{a.title}</b>
          <div className="small muted">ПВЗ: {pvz ? [pvz.id, pvz.address].join(" · ") : "—"}</div></div>
        <div className="buy-kv">
          <span>Кошелёк WB<b>{rubk(pa.funds)}</b></span><span>Оплата сейчас<b>{rubk(nowSum)}</b></span>
          <span>При получении<b>{rubk(postSum)}</b></span>
        </div>
      </div>
      {used != null && <div className="small muted">Лимит оплаты при получении: использовано {rubk(used)} из {rubk(pa.postpaid_limit)} ·
        доступно {rubk(pa.postpaid_left)}{pa.prepaid_only ? " · аккаунту доступна только предоплата" : ""}</div>}
      {pa.limit_left != null && <div className="small muted">Лимит скидки Кошелька: осталось {rubk(pa.limit_left)}</div>}
      {pa.error && <div className="bad-t">{pa.error}</div>}
      {rows.map((r) => <BuyLine key={r.ln.nm} r={r} pa={pa} setPay={(v) => setPay(key(a.id, r.ln.nm), v)} />)}
      {lack && <CertAdd a={a} need onDone={refresh} />}
    </div>
  );
}

// ---------- итог ----------
function BuyResult({ o, cancel }: { o: BuyOrder; cancel: (o: BuyOrder) => void }) {
  const st = o.status;
  return (
    <li className={st === "error" ? "bad-t" : st === "unknown" ? "up-t" : ""}>
      <span className="st">{st === "ok" ? "✅" : st === "error" ? "❌" : st === "unknown" ? "⚠️" : st === "cancelled" ? "↩" : "⏳"}</span>
      {o.name ?? `Товар ${o.nm}`} — {pcs(o.qty)}
      {(st === "pending" || st === "sending") && <span className="muted"> · <span className="spin" /> заказываю…</span>}
      {st === "ok" && <span className="ok-t"> · заказано, {o.pay_now ? "оплачено Кошельком" : "оплата при получении"}</span>}
      {st === "cancelled" && <span className="muted"> · заказ отменён</span>}
      {(st === "error" || st === "unknown") && o.error && <div className="small">{o.error}</div>}
      {o.cancel_state === "pending" && <div className="small muted"><span className="spin" /> Отменяю…</div>}
      {o.cancel_state === "error" && <div className="small bad-t">Отмена не прошла: {o.cancel_error}</div>}
      {o.can_cancel && <div><button className="link" onClick={() => cancel(o)}>Отменить заказ</button></div>}
    </li>
  );
}

function Results({ ids, all, opts, onClose }: { ids: number[]; all: Account[]; opts: PickupPoint[]; onClose: () => void }) {
  const qc = useQueryClient();
  const done = (o: BuyOrder) => FINAL.includes(o.status) && o.cancel_state !== "pending";
  const q = useQuery({ queryKey: ["buy", ids.join()], queryFn: () => api<BuyOrder[]>(`/order/status?${ids.map((i) => `ids=${i}`).join("&")}`),
    refetchInterval: (x) => (x.state.data?.length === ids.length && x.state.data.every(done) ? false : 1500) });
  const list = q.data ?? [];
  const finished = list.length === ids.length && list.every(done);
  const okN = list.filter((o) => o.status === "ok").length;
  const [msg, setMsg] = useState("");
  useEffect(() => { if (finished) qc.invalidateQueries({ queryKey: ["accounts"] }); }, [finished, okN]);
  const cancel = async (o: BuyOrder) => {
    const a = all.find((x) => x.id === o.account_id);
    if (!window.confirm(`Отменить заказ «${o.name ?? o.nm}» (${pcs(o.qty)}) на аккаунте ${a?.title ?? ""}?`)) return;
    setMsg("");
    try { await api("/order/cancel", { method: "POST", body: { id: o.id } }); await q.refetch(); } catch (e) { setMsg(err(e)); }
  };
  const accIds = [...new Set(list.map((o) => o.account_id))];
  return (
    <>
      <h2>{!finished ? <><span className="spin" /> Заказываю… {list.filter(done).length} из {ids.length}</>
        : <>Заказано {okN} из {ids.length}{okN === ids.length && <span className="ok-t"> ✓</span>}</>}</h2>
      <div className="cert-sum">
        {accIds.map((id) => {
          const a = all.find((x) => x.id === id);
          const mine = list.filter((o) => o.account_id === id);
          const pvz = opts.find((p) => p.id === mine[0]?.pvz_id);
          return (
            <div key={String(id)} className="card sm">
              <div><b>{a?.title ?? "аккаунт удалён"}</b></div>
              {pvz && <div className="small muted">ПВЗ: {pvz.id} · {pvz.address}</div>}
              <ul className="cert-lines">{mine.map((o) => <BuyResult key={o.id} o={o} cancel={cancel} />)}</ul>
            </div>
          );
        })}
      </div>
      {msg && <p className="err">{msg}</p>}
      {!finished && <p className="muted small">Можно закрыть окно — заказы продолжатся. Итог будет в заказах аккаунтов.</p>}
      <div className="wiz-foot"><span /><button className="btn" onClick={onClose}>{finished ? "Готово" : "Закрыть"}</button></div>
    </>
  );
}

// ---------- мастер ----------
type Bad = { row: number; error: string }[];
const badItems = (e: unknown): Bad | null => {
  const d = e instanceof ApiError ? (e.detail as { code?: string; items?: Bad } | null) : null;
  return d?.code === "bad_items" && d.items ? d.items : null;
};

function Wizard({ all, onClose }: { all: Account[]; onClose: () => void }) {
  const [step, setStep] = useState(1);
  const [picked, setPicked] = useState<number[]>([]);
  const [prods, setProds] = useState<ProductCard[]>([]);
  const [qty, setQty] = useState<Qty>({});
  const [opts, setOpts] = useState<PickupPoint[]>([]);
  const [pvzOf, setPvzOf] = useState<Record<number, number>>({});
  const [prev, setPrev] = useState<{ id: number; data: Preview } | null>(null);
  const [prevErr, setPrevErr] = useState("");
  const [pay, setPay] = useState<Record<string, boolean>>({});
  const [server, setServer] = useState<Record<string, string>>({});  // причины строк от API (422)
  const [placeErr, setPlaceErr] = useState<{ text: string; refresh: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [ids, setIds] = useState<number[] | null>(null);
  const accs = all.filter((a) => picked.includes(a.id));
  const has = (a: Account) => prods.some((p) => (qty[p.nm]?.[a.id] ?? 0) > 0);
  const buyers = accs.filter(has);
  const skipped = accs.filter((a) => !has(a));
  const lines = buyers.flatMap((a) => prods.filter((p) => (qty[p.nm]?.[a.id] ?? 0) > 0)
    .map((p) => ({ account_id: a.id, nm: p.nm, qty: qty[p.nm][a.id], pvz_id: pvzOf[a.id] })));
  const pvzSet = buyers.filter((a) => pvzOf[a.id]).length;

  const preview = async () => {
    setPrev(null); setPrevErr(""); setServer({}); setPlaceErr(null);
    try {
      const { id, result } = await runQuote<Preview>("/order/preview", { lines });
      setPrev({ id, data: result });
    } catch (e) { setPrevErr(err(e)); }
  };
  const plans = prev ? plan(buyers, lines, prev.data, pay, server, Object.fromEntries(prods.map((p) => [p.nm, p.name]))) : [];
  const rows = plans.flatMap((p) => p.rows.map((r) => ({ a: p.a, r })));
  const ready = rows.filter(({ r }) => !r.why);
  const nowSum = sum(ready.filter(({ r }) => r.payNow).map(({ r }) => r.ln.now ?? 0));
  const postSum = sum(ready.filter(({ r }) => !r.payNow).map(({ r }) => (r.ln.total ?? 0) * r.ln.qty));

  const place = async () => {
    setBusy(true); setPlaceErr(null);
    const items = ready.map(({ a, r }) => ({ account_id: a.id, nm: r.ln.nm, qty: r.ln.qty, pvz_id: r.ln.pvz_id, pay_now: r.payNow }));
    try {
      const res = await api<{ ids: number[] }>("/order/place", { method: "POST",
        body: { quote_id: prev!.id, items, confirm_total: nowSum + postSum } });
      setIds(res.ids);
    } catch (e) {
      const bad = badItems(e);
      if (bad) setServer(Object.fromEntries(bad.map((b) => [key(items[b.row].account_id, items[b.row].nm), b.error])));
      else setPlaceErr({ text: err(e), refresh: e instanceof ApiError && e.status === 409 });
    } finally { setBusy(false); }
  };

  if (ids) return <Results ids={ids} all={all} opts={opts} onClose={onClose} />;
  const hint = step === 1 ? (picked.length ? `Выбрано аккаунтов: ${picked.length}` : "Выберите хотя бы один аккаунт")
    : step === 2 ? (!prods.length ? "Добавьте товар" : !buyers.length ? "Укажите количество хотя бы для одного аккаунта"
      : skipped.length ? `Без товаров: ${skipped.map((a) => a.title).join(", ")} — пропустим` : `Аккаунтов: ${buyers.length} · ${pcs(sum(lines.map((l) => l.qty)))}`)
    : step === 3 ? `Выбран пункт у ${pvzSet} из ${buyers.length} аккаунтов` : "";
  return (
    <>
      <h2>Заказать товар</h2>
      <Steps step={step} />
      {step === 1 && <StepAccounts all={all} picked={picked} setPicked={setPicked}
        text="Выберите аккаунты, на которые будет заказан товар. Можно один или несколько." />}
      {step === 2 && <>
        <p className="muted">Найдите товар по артикулу или ссылке и укажите, сколько штук заказать на каждый аккаунт.</p>
        {prods.map((p) => <ProductQty key={p.nm} p={p} accs={accs} mine={qty[p.nm] ?? {}}
          setN={(acc, n) => setQty({ ...qty, [p.nm]: { ...qty[p.nm], [acc]: n } })}
          onRemove={() => setProds(prods.filter((x) => x.nm !== p.nm))} />)}
        {prods.length > 0 && <div className="small muted">Добавить ещё товар:</div>}
        <FindProduct have={prods.map((p) => p.nm)} onAdd={(p) => setProds([...prods, p])} />
      </>}
      {step === 3 && <StepPickups all={all} accs={buyers} prods={prods} qty={qty} opts={opts} setOpts={setOpts}
        pvzOf={pvzOf} setPvzOf={setPvzOf} />}
      {step === 4 && <>
        <p className="muted">Проверьте заказ. «При получении» идёт в лимит оплаты при получении аккаунта; «Оплатить сейчас» —
          с Кошелька WB сразу. Строки, которые нельзя заказать, — серые: их не заказываем.</p>
        {!prev && !prevErr && <p><span className="spin" /> Считаю цены на выбранные пункты выдачи…</p>}
        {prevErr && <p className="err">{prevErr} <button className="link" onClick={preview}>Попробовать ещё раз</button></p>}
        {prev && <div className="card buy">
          {plans.map((p) => <BuyAccount key={p.a.id} p={p} no={all.indexOf(p.a) + 1} pvz={opts.find((x) => x.id === pvzOf[p.a.id])}
            setPay={(k, v) => { setPay({ ...pay, [k]: v }); setServer({}); }} refresh={preview} />)}
        </div>}
        {placeErr && <p className="err">{placeErr.text} {placeErr.refresh && <button className="link" onClick={preview}>Обновить цены</button>}</p>}
      </>}
      <div className="wiz-foot">
        <span className="small muted">
          {step < 4 ? hint : prev && <>
            {ready.length < rows.length && <b className="up-t">Будет заказано {ready.length} из {rows.length} товаров · </b>}
            К оплате сейчас: {rubk(nowSum)} · при получении: {rubk(postSum)}</>}
        </span>
        <span className="wiz-btns">
          {step === 1 ? <button className="link" onClick={onClose}>Отмена</button>
            : <button className="btn ghost" onClick={() => setStep(step - 1)}>Назад</button>}
          {step === 1 && <button className="btn" disabled={!picked.length} onClick={() => setStep(2)}>Далее</button>}
          {step === 2 && <button className="btn" disabled={!buyers.length} onClick={() => setStep(3)}>Далее</button>}
          {step === 3 && <button className="btn" disabled={pvzSet < buyers.length}
            onClick={() => { setStep(4); preview(); }}>Далее</button>}
          {step === 4 && <button className="btn" disabled={!prev || !ready.length || busy} onClick={place}>
            Заказать · {rubk(nowSum + postSum)}</button>}
        </span>
      </div>
    </>
  );
}

export default function OrderModal({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  if (!open) return null;
  // фон не закрывает: случайный клик мимо стёр бы набранный заказ
  return <Modal label="Заказать товар" onClose={onClose} wide><Wizard all={all} onClose={onClose} /></Modal>;
}
