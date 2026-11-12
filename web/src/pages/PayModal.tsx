// «Оплата товаров» с WB Кошелька: окно на «Аккаунтах» (шаг 1 — выбор, шаг 2 — выбранные с сертификатом в строке)
// и окно ПВЗ на странице аккаунта. Платит worker (очередь по прокси) — окна только создают оплату и опрашивают статус.
// Сумму считает сервер по нашим ценам; confirm показывает её же (не совпала — 409, ничего не платим).
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, time, type Account, type Cert, type Order, type Payment } from "../api";
import { Blocked, confirmTopUp, LimitNote, Modal, unpaid, visible, type Pvz } from "../accounts";
import { NOMINALS, normCode, parseNominal, payOverLimit } from "../certs";

const FINAL = ["ok", "error", "unknown"];
const err = (e: unknown) => (e instanceof ApiError ? e.message : "ошибка");
const total = (os: Order[]) => os.reduce((s, o) => s + (o.price ?? 0), 0);
/** Что платим у аккаунта: неоплаченные видимые (как «Не оплачено»). */
export const toPay = (a: Account, orders: Order[] = a.orders) => visible(orders).filter((o) => o.paid === false);
const payable = (a: Account) => a.status === "active" && !a.wallet_blocked;

/** Оплаты окна: account_id → последняя оплата; опрос, пока не закончатся; по итогу — свежие аккаунты. */
function usePayments() {
  const qc = useQueryClient();
  const [ids, setIds] = useState<Record<number, number>>({});
  const list = Object.values(ids);
  const q = useQuery({ queryKey: ["payments", list.join()], enabled: list.length > 0,
    queryFn: () => api<Payment[]>(`/payments?${list.map((i) => `ids=${i}`).join("&")}`),
    refetchInterval: (q) => (q.state.data?.every((p) => FINAL.includes(p.status)) ? false : 1500) });
  const byAcc: Record<number, Payment> = {};
  for (const p of q.data ?? []) if (p.account_id != null && ids[p.account_id] === p.id) byAcc[p.account_id] = p;
  const done = Object.values(byAcc).filter((p) => FINAL.includes(p.status)).length;
  useEffect(() => { if (done) qc.invalidateQueries({ queryKey: ["accounts"] }); }, [done]);
  const pay = async (a: Account, orders: Order[]) => {
    const r = await api<{ id: number }>("/payments", { method: "POST",
      body: { account_id: a.id, rids: orders.map((o) => o.rid), confirm_amount: total(orders) } });
    setIds((x) => ({ ...x, [a.id]: r.id }));
  };
  const busy = (id: number) => (!!byAcc[id] && !FINAL.includes(byAcc[id].status)) || (!!ids[id] && !byAcc[id]);
  return { byAcc, pay, busy };
}

/** Итог оплаты: «✓ Оплачено N шт., списано X ₽»; списали больше «К оплате» — красным. */
export function PayStatus({ p }: { p: Payment }) {
  if (!FINAL.includes(p.status)) return <div className="small"><span className="spin" /> Оплачиваю… {rub(p.amount)}</div>;
  if (p.status !== "ok") return <div className={`small ${p.status === "error" ? "bad-t" : "up-t"}`}>
    {p.status === "error" ? "❌" : "⚠️"} {p.error_text ?? "ошибка"}</div>;
  const off = p.funds_before != null && p.funds_after != null ? p.funds_before - p.funds_after : null;
  return (
    <div className="small">
      <span className="ok-t">✓ Оплачено {p.rids.length} шт., списано {rub(off)}</span>
      {off != null && off > p.amount && <div className="red-box">WB списал больше, чем «К оплате»: {rub(off)} вместо {rub(p.amount)}</div>}
    </div>
  );
}

/** Сертификат прямо в строке: код + номинал → та же очередь активации, что в мастере; текст лимита — тот же.
 *  need — денег не хватает; хватило после активации — поля прячутся, итог активации остаётся. */
export function CertAdd({ a, need }: { a: Account; need: boolean }) {
  const qc = useQueryClient();
  const [code, setCode] = useState("");
  const [nominal, setNominal] = useState("");
  const [id, setId] = useState<number | null>(null);
  const [msg, setMsg] = useState("");
  const n = parseNominal(nominal) ?? 0;
  const cert = useQuery({ queryKey: ["certs", String(id)], enabled: id != null,
    queryFn: () => api<Cert[]>(`/certificates?ids=${id}`),
    refetchInterval: (q) => (q.state.data?.every((c) => FINAL.includes(c.status)) ? false : 1500) });
  const c = cert.data?.[0];
  useEffect(() => { if (c && FINAL.includes(c.status)) qc.invalidateQueries({ queryKey: ["accounts"] }); }, [c?.status]);
  const add = useMutation({
    mutationFn: () => api<{ ids: number[] }>("/certificates", { method: "POST",
      body: { items: [{ code: normCode(code) ?? code, nominal: n, account_id: a.id }] } }),
    onMutate: () => setMsg(""),
    onSuccess: (r) => { setId(r.ids[0]); setCode(""); setNominal(""); },
    onError: (e: unknown) => {
      const d = e instanceof ApiError ? (e.detail as { items?: { error: string }[] } | null) : null;
      setMsg(d?.items?.[0]?.error ?? err(e));
    },
  });
  const sending = c && !FINAL.includes(c.status);
  if (!need && id == null) return null;
  return (
    <div className="cert-add">
      {need && <>{id == null && <div className="cert-add-row">
        <input aria-label="Код сертификата" placeholder="Код сертификата" spellCheck={false} value={code}
          onChange={(e) => setCode(e.target.value)} />
        <input aria-label="Номинал ₽" list="pay-nominals" inputMode="numeric" placeholder="Номинал ₽" value={nominal}
          onChange={(e) => setNominal(e.target.value)} />
        <datalist id="pay-nominals">{NOMINALS.map((x) => <option key={x} value={x} />)}</datalist>
        <button className="btn sm" disabled={!normCode(code) || !n || add.isPending || !!sending}
          onClick={() => confirmTopUp([{ a, n }]) && add.mutate()}>
          Добавить сертификат</button>
      </div>}
      {id != null && !sending && <button className="link" onClick={() => setId(null)}>+ ещё сертификат</button>}
      <LimitNote a={a} n={n} /></>}
      {msg && <div className="bad-t small">{msg}</div>}
      {c && <div className={`small ${c.status === "ok" ? "ok-t" : c.status === "error" ? "bad-t" : c.status === "unknown" ? "up-t" : ""}`}>
        {c.status === "ok" ? `✅ ${c.code.slice(-4)}: зачислено ${rub(c.nominal)}, на кошельке ${rub(c.funds)}`
          : FINAL.includes(c.status) ? `${c.status === "error" ? "❌" : "⚠️"} ${c.error_text ?? "ошибка"}`
          : <><span className="spin" /> Активирую сертификат…</>}</div>}
    </div>
  );
}

/** Статус денег аккаунта для суммы: хватает / не хватает X / неоплаченных нет. */
function Money({ a, sum, ok = "Денег хватает" }: { a: Account; sum: number; ok?: string }) {
  if (!sum) return <span className="muted">Неоплаченных нет</span>;
  const lack = sum - (a.funds ?? 0);
  return lack > 0 ? <span className="up-t">Не хватает {rub(lack)}</span> : <span className="ok-t">{ok}</span>;
}

// ---------- «Оплата товаров» (меню «Функции») ----------
function Step1({ all, picked, setPicked }: { all: Account[]; picked: number[]; setPicked: (p: number[]) => void }) {
  const list = all.filter((a) => unpaid(a) > 0);
  const toggle = (id: number) => setPicked(list.map((a) => a.id).filter((x) => (x === id) !== picked.includes(x)));
  if (!list.length) return <p className="empty">Неоплаченных заказов нет</p>;
  return (
    <>
      <div className="filters">
        <button className="link" onClick={() => setPicked(list.filter(payable).map((a) => a.id))}>Выбрать все</button>
        <button className="link" onClick={() => setPicked([])}>Снять выбор</button>
      </div>
      <div className="table-wrap">
        <table className="cert-accs pay-accs">
          <thead><tr><th /><th>Аккаунт</th><th className="num">Товаров</th><th className="num">К оплате</th>
            <th className="num">Кошелёк WB</th><th>Статус</th></tr></thead>
          <tbody>
            {list.map((a) => {
              const can = payable(a);
              return (
                <tr key={a.id} className={`${a.wallet_blocked ? "blocked" : ""} ${can ? "click" : "off"}`} onClick={() => can && toggle(a.id)}>
                  <td><input type="checkbox" aria-label={a.title} disabled={!can} checked={picked.includes(a.id)}
                    onChange={() => toggle(a.id)} onClick={(e) => e.stopPropagation()} /></td>
                  <td><b>{a.title}</b></td>
                  <td className="num">{toPay(a).length}</td>
                  <td className="num">{rub(unpaid(a))}</td>
                  <td className="num money">{rub(a.funds)}</td>
                  <td>{a.wallet_blocked ? <Blocked a={a} /> : a.status !== "active" ? <span className="pill bad">нужен вход</span>
                    : <Money a={a} sum={unpaid(a)} ok="Можно оплатить" />}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Step2Row({ a, i, onRemove, p }: { a: Account; i: number; onRemove: () => void; p?: Payment }) {
  const qc = useQueryClient();
  const poll = useMutation({ mutationFn: () => api(`/accounts/${a.id}/poll`, { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["accounts"] }) });
  const u = unpaid(a);
  const over = payOverLimit(u, a.limit_left);
  return (
    <div className="card sm pay-row">
      <div className="pay-head">
        <span className="no muted">{i + 1}</span>
        <span><b>{a.title}</b> <span className="muted small">Обновлено {a.polling ? <span className="spin" /> : time(a.state_at)}</span></span>
        <span className="small">Кошелёк WB <b className="money">{rub(a.funds)}</b></span>
        <span className="small">Не оплачено <b className={u ? "up-t" : ""}>{rub(u)}</b></span>
        <span className="small"><Money a={a} sum={u} /></span>
        <span className="pay-act">
          <button className="link" disabled={a.polling || poll.isPending} onClick={() => poll.mutate()}>↻ Обновить</button>
          <button className="link" onClick={onRemove}>Убрать</button>
        </span>
      </div>
      {u > 0 && over && <div className="red-box small">{over}</div>}
      {!(p && !FINAL.includes(p.status)) && <CertAdd a={a} need={u > (a.funds ?? 0)} />}
      {p && <PayStatus p={p} />}
    </div>
  );
}

function PayWizard({ all, onClose }: { all: Account[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [step, setStep] = useState(1);
  const [picked, setPicked] = useState<number[]>([]);
  const [error, setError] = useState("");
  const pays = usePayments();
  const accs = all.filter((a) => picked.includes(a.id));
  const ready = accs.filter((a) => payable(a) && unpaid(a) > 0 && (a.funds ?? 0) >= unpaid(a) && !pays.busy(a.id));
  const sum = ready.reduce((s, a) => s + unpaid(a), 0);
  const noMoney = accs.filter((a) => unpaid(a) > (a.funds ?? 0)).length;
  const refreshAll = useMutation({
    mutationFn: () => Promise.allSettled(accs.map((a) => api(`/accounts/${a.id}/poll`, { method: "POST" }))),
    onSettled: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });
  const payAll = async () => {
    if (!confirm(`Оплатить с WB Кошелька ${ready.length} акк. на ${rub(sum)}?\n\n`
      + ready.map((a) => `${a.title} — ${rub(unpaid(a))}`).join("\n"))) return;
    setError("");
    const fails: string[] = [];
    for (const a of ready) {
      try { await pays.pay(a, toPay(a)); } catch (e) { fails.push(`${a.title}: ${err(e)}`); }
    }
    if (fails.length) setError(fails.join("; "));
  };
  return (
    <>
      <h2>Оплата товаров</h2>
      {step === 1 ? <>
        <p className="muted">Аккаунты с неоплаченными заказами. Оплата — с WB Кошелька аккаунта.</p>
        <Step1 all={all} picked={picked} setPicked={setPicked} />
      </> : <>
        <div className="filters">
          <b>Выбранные аккаунты</b>
          <button className="link" disabled={refreshAll.isPending} onClick={() => refreshAll.mutate()}>↻ Обновить все выбранные</button>
        </div>
        {accs.map((a, i) => <Step2Row key={a.id} a={a} i={i} p={pays.byAcc[a.id]}
          onRemove={() => setPicked(picked.filter((x) => x !== a.id))} />)}
        {!accs.length && <p className="empty">Никого не осталось — вернитесь и выберите аккаунты</p>}
      </>}
      {error && <p className="err">{error}</p>}
      <div className="wiz-foot">
        <span className="small muted">{step === 1 ? (picked.length ? `Выбрано аккаунтов: ${picked.length}` : "Выберите аккаунты, которые нужно оплатить")
          : `В группе: ${accs.length} · без денег: ${noMoney} · можно оплатить: ${ready.length}`}</span>
        <span className="wiz-btns">
          {step === 1 ? <>
            <button className="link" onClick={onClose}>Отмена</button>
            <button className="btn" disabled={!picked.length} onClick={() => setStep(2)}>Далее →</button>
          </> : <>
            <button className="btn ghost" onClick={() => setStep(1)}>← Назад</button>
            <button className="btn" disabled={!ready.length} onClick={payAll}>
              {ready.length ? `Оплатить всем доступным · ${rub(sum)}` : "Нет доступных для оплаты"}</button>
          </>}
        </span>
      </div>
    </>
  );
}

export default function PayModal({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  if (!open) return null;
  return <Modal label="Оплата товаров" onClose={onClose} wide><PayWizard all={all} onClose={onClose} /></Modal>;
}

// ---------- окно ПВЗ на странице аккаунта ----------
export function PvzPay({ a, g, onClose }: { a: Account; g: Pvz; onClose: () => void }) {
  const list = toPay(a, g.orders);
  const [off, setOff] = useState<Set<string>>(new Set());  // снятые галочки
  const [error, setError] = useState("");
  const pays = usePayments();
  const p = pays.byAcc[a.id];
  const chosen = list.filter((o) => !off.has(o.rid));
  const allBox = useRef<HTMLInputElement>(null);
  useEffect(() => { if (allBox.current) allBox.current.indeterminate = chosen.length > 0 && chosen.length < list.length; });
  const sum = total(chosen);
  const lack = sum > (a.funds ?? 0);
  const over = payOverLimit(sum, a.limit_left);
  const go = async () => {
    if (!confirm(`Оплатить с WB Кошелька ${a.title}: ${chosen.length} шт. на ${rub(sum)}?`)) return;
    setError("");
    try { await pays.pay(a, chosen); } catch (e) { setError(err(e)); }
  };
  return (
    <Modal label="Оплатить товары" onClose={onClose}>
      <h2>{a.title} · {g.address}</h2>
      <div className="kv pay-kv">
        <div><span>Неоплаченных товаров</span><b>{list.length}</b></div>
        <div><span>К оплате</span><b>{rub(sum)}</b></div>
        <div><span>Кошелёк WB</span><b className="money">{rub(a.funds)}</b></div>
        <div><span>Статус</span><b>{a.wallet_blocked ? <Blocked a={a} />
          : list.length && !chosen.length ? <span className="muted">Ничего не выбрано</span> : <Money a={a} sum={sum} />}</b></div>
      </div>
      {list.length > 1 && (
        <ul className="pay-items">
          <li className="pay-all"><label>
            <input ref={allBox} type="checkbox" checked={chosen.length === list.length}
              onChange={() => setOff(chosen.length === list.length ? new Set(list.map((o) => o.rid)) : new Set())} /> Выбрать все</label></li>
          {list.map((o) => (
            <li key={o.rid}><label>
              <input type="checkbox" checked={!off.has(o.rid)} onChange={() => {
                const s = new Set(off); s.has(o.rid) ? s.delete(o.rid) : s.add(o.rid); setOff(s);
              }} /> {o.name ?? `арт. ${o.nm_id}`} <b className="num">{rub(o.price)}</b></label></li>
          ))}
        </ul>
      )}
      {over && <div className="red-box small">{over}</div>}
      {lack && !p && <p className="up-t small">Денег не хватает. Добавьте сертификат прямо здесь — после зачисления можно оплатить.</p>}
      {!p && <CertAdd a={a} need={lack} />}
      {p && <PayStatus p={p} />}
      {error && <p className="err">{error}</p>}
      <div className="wiz-foot">
        <button className="link" onClick={onClose}>Закрыть</button>
        <button className="btn" disabled={!chosen.length || lack || !payable(a) || pays.busy(a.id) || p?.status === "ok"} onClick={go}>
          Оплатить {rub(sum)}</button>
      </div>
    </Modal>
  );
}
