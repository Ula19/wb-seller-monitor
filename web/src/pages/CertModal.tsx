// «🎁 Добавить сертификат»: мастер из 3 шагов (Аккаунты → Сертификаты → Проверка), потом активация с живыми
// статусами и итог. Активирует worker (очередь по прокси, ~3 с на код) — модалка только опрашивает статусы.
import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, rub, type Account, type Cert } from "../api";
import { Blocked, Limit, visible } from "../accounts";
import { distribute, NOMINAL_MAX, NOMINAL_MIN, NOMINALS, normCode, parseList, parseNominal, type BadLine } from "../certs";

// certId/orig — строка из неудачных прошлого круга («← Назад»): код не меняли — retry той же записи, поменяли — новый код
type Row = { key: number; code: string; nominal: string; account: number | null; certId?: number; orig?: string };
type Known = Record<string, { status: string; title: string | null; done_at: string }>;
let seq = 0;
const newRow = (r: Partial<Row> = {}): Row => ({ key: ++seq, code: "", nominal: "", account: null, ...r });
const unpaid = (a: Account) => visible(a.orders).filter((o) => o.paid === false).reduce((s, o) => s + (o.price ?? 0), 0);
const sum = (xs: number[]) => xs.reduce((s, x) => s + x, 0);
const pcs = (n: number) => `${n} шт.`;
const FINAL = ["ok", "error", "unknown"];

function Steps({ step }: { step: number }) {
  return (
    <ol className="steps">
      {["Аккаунты", "Сертификаты", "Проверка"].map((t, i) => (
        <li key={t} className={i + 1 === step ? "on" : i + 1 < step ? "done" : ""}><b>{i + 1 < step ? "✓" : i + 1}</b>{t}</li>
      ))}
    </ol>
  );
}

// ---------- шаг 1 ----------
function StepAccounts({ all, picked, setPicked }: { all: Account[]; picked: number[]; setPicked: (p: number[]) => void }) {
  const [q, setQ] = useState("");
  const can = (a: Account) => a.status === "active";
  const list = all.filter((a) => [a.title, a.name, a.label].some((t) => t?.toLowerCase().includes(q.trim().toLowerCase())));
  // порядок выбранных = порядок списка: по нему «Распределить поровну»
  const toggle = (id: number) => setPicked(all.map((a) => a.id).filter((x) => (x === id) !== picked.includes(x)));
  return (
    <>
      <p className="muted">Выберите аккаунты, на которые будут добавлены сертификаты. Можно один или несколько.</p>
      <div className="filters">
        <input className="search" placeholder="Поиск по имени" value={q} onChange={(e) => setQ(e.target.value)} />
        <button className="link" onClick={() => setPicked(all.filter((a) => can(a) && (picked.includes(a.id) || list.includes(a))).map((a) => a.id))}>Выбрать все</button>
        <button className="link" onClick={() => setPicked([])}>Снять выбор</button>
      </div>
      <div className="table-wrap">
        <table className="cert-accs">
          <thead><tr><th /><th>Аккаунт</th><th>Остаток лимита</th><th className="num">Кошелёк WB</th><th className="num">Не оплачено</th></tr></thead>
          <tbody>
            {list.map((a) => {
              const u = unpaid(a);
              return (
                <tr key={a.id} className={`${a.wallet_blocked ? "blocked" : ""} ${can(a) ? "click" : "off"}`} onClick={() => can(a) && toggle(a.id)}>
                  <td><input type="checkbox" aria-label={a.title} disabled={!can(a)} checked={picked.includes(a.id)} onChange={() => toggle(a.id)} onClick={(e) => e.stopPropagation()} /></td>
                  <td><b>{a.title}</b> {!can(a) && <span className="pill bad">нужен вход</span>} <Blocked a={a} />
                    {a.proxy_down && <span className="pill warn">прокси не отвечает</span>}</td>
                  <td className="lim"><Limit a={a} short /></td>
                  <td className="num">{rub(a.funds)}</td>
                  <td className="num">{u ? <span className="up-t">{rub(u)}</span> : <span className="muted">—</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ---------- шаг 2 ----------
function rowProblem(r: Row, dup: Set<string>, known: Known, server: Record<number, string>): string | null {
  if (server[r.key]) return server[r.key];
  const c = normCode(r.code);
  if (!r.code.trim()) return null;
  if (!c) return "неверный код — нужно 16 латинских букв и цифр";
  if (dup.has(c)) return "код повторяется в списке";
  const k = r.certId && c === r.orig ? undefined : known[c];  // свой же неудачный код — не «уже добавлен»
  if (k) return k.status === "ok" ? `уже активирован${k.title ? `: ${k.title}` : ""}, ${new Date(k.done_at).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })}`
    : "уже добавлен — ждёт активации или проверки баланса";
  return null;
}

function StepCerts({ accs, rows, setRows, problems }: {
  accs: Account[]; rows: Row[]; setRows: (r: Row[]) => void; problems: Record<number, string | null>;
}) {
  const [paste, setPaste] = useState<string | null>(null);
  const [bad, setBad] = useState<BadLine[]>([]);
  const set = (key: number, v: Partial<Row>) => setRows(rows.map((r) => (r.key === key ? { ...r, ...v } : r)));
  const parse = () => {
    const p = parseList(paste ?? "");
    const filled = rows.filter((r) => r.code.trim() || r.nominal.trim());
    setRows([...filled, ...p.rows.map((x) => newRow({ code: x.code, nominal: x.nominal ? String(x.nominal) : "" }))]);
    setBad(p.bad);
    setPaste(null);
  };
  const per = accs.map((a) => {
    const mine = rows.filter((r) => r.account === a.id);
    return { a, n: mine.length, s: sum(mine.map((r) => parseNominal(r.nominal) ?? 0)) };
  });
  const orphan = rows.filter((r) => r.account == null && (r.code.trim() || r.nominal.trim())).length;
  return (
    <>
      <p className="muted">Впишите код сертификата (16 символов) и его номинал. Для каждого выберите, на какой аккаунт он пойдёт.</p>
      <div className="filters">
        <button className="btn ghost sm" onClick={() => setPaste(paste == null ? "" : null)}>Вставить списком</button>
        <button className="btn ghost sm" disabled={!rows.length} onClick={() => setRows(distribute(rows, accs.map((a) => a.id)))}>Распределить поровну</button>
      </div>
      {paste != null && (
        <div className="paste">
          <textarea autoFocus rows={6} placeholder={"По одному в строке: UYPMJUAFGZEVR89S 1000"} value={paste} onChange={(e) => setPaste(e.target.value)} />
          <div><button className="btn sm" disabled={!paste.trim()} onClick={parse}>Разобрать список</button>{" "}
            <button className="link" onClick={() => setPaste(null)}>Отмена</button></div>
        </div>
      )}
      {bad.length > 0 && (
        <div className="bad-lines small">
          <b>Не разобрано строк: {bad.length}</b> <button className="link" onClick={() => setBad([])}>скрыть</button>
          <ul>{bad.map((b, i) => <li key={i}><code>{b.line}</code> — {b.why}</li>)}</ul>
        </div>
      )}
      <datalist id="cert-nominals">{NOMINALS.map((n) => <option key={n} value={n} />)}</datalist>
      <div className="crows">
        <div className="crow head muted small"><span>Код</span><span>Номинал, ₽</span><span>Аккаунт</span><span /></div>
        {rows.map((r) => {
          const n = parseNominal(r.nominal);
          const p = problems[r.key];
          return (
            <div key={r.key} className={`crow ${p ? "bad" : ""}`}>
              <input aria-label="Код" placeholder="16 символов" value={r.code} spellCheck={false}
                onChange={(e) => set(r.key, { code: e.target.value })}
                onBlur={() => { const c = normCode(r.code); if (c && c !== r.code) set(r.key, { code: c }); }} />
              <input aria-label="Номинал" list="cert-nominals" inputMode="numeric" placeholder="1000" value={r.nominal}
                onChange={(e) => set(r.key, { nominal: e.target.value })} />
              <select aria-label="Аккаунт" value={r.account ?? ""} onChange={(e) => set(r.key, { account: e.target.value ? Number(e.target.value) : null })}>
                <option value="">— аккаунт —</option>
                {accs.map((a) => <option key={a.id} value={a.id}>{a.title}</option>)}
              </select>
              <button className="x" aria-label="Убрать строку" onClick={() => setRows(rows.filter((x) => x.key !== r.key))}>✕</button>
              {p && <div className="why bad-t small">{p}</div>}
              {!p && r.nominal.trim() && n == null && <div className="why bad-t small">номинал — целое число больше 0</div>}
              {!p && n != null && (n < NOMINAL_MIN || n > NOMINAL_MAX) && <div className="why up-t small">необычный номинал — WB продаёт от 500 до 100 000 ₽; проверьте</div>}
            </div>
          );
        })}
      </div>
      <button className="link" onClick={() => setRows([...rows, newRow({ account: accs.length === 1 ? accs[0].id : null })])}>+ Ещё сертификат</button>
      <div className="cert-cards">
        {per.map(({ a, n, s }) => (
          <div key={a.id} className="card sm"><b>{a.title}</b><div className="small">{n ? `${pcs(n)} · ${rub(s)}` : <span className="muted">пока нет сертификатов</span>}</div></div>
        ))}
        {orphan > 0 && <div className="card sm warn"><b>Без аккаунта: {pcs(orphan)}</b><div className="small">выберите аккаунт</div></div>}
      </div>
    </>
  );
}

// ---------- шаг 3 и итог ----------
type Item = { code: string; nominal: number; account: number };

function Summary({ accs, items, certs }: { accs: Account[]; items: Item[]; certs: Cert[] | null }) {
  const byCode = new Map((certs ?? []).map((c) => [c.code, c]));
  return (
    <div className="cert-sum">
      {accs.map((a) => {
        const mine = items.filter((i) => i.account === a.id);
        if (!mine.length) return null;
        const s = sum(mine.map((i) => i.nominal));
        const got = mine.map((i) => byCode.get(i.code)).filter((c): c is Cert => !!c);
        const ok = got.filter((c) => c.status === "ok");
        const done = certs && got.length === mine.length && got.every((c) => FINAL.includes(c.status));
        const funds = got.find((c) => c.funds != null)?.funds ?? a.funds;
        return (
          <div key={a.id} className="card sm">
            <div><b>{a.title}</b> <Blocked a={a} /></div>
            <div className="small muted">
              {!certs ? `${pcs(mine.length)} · ${rub(s)} · баланс станет ${rub((a.funds ?? 0) + s)}`
                : `${pcs(ok.length)} · ${rub(sum(ok.map((c) => c.nominal ?? 0)))}${done ? ` · баланс теперь ${rub(funds)}` : ""}`}
            </div>
            <ul className="cert-lines">
              {mine.map((i) => {
                const c = byCode.get(i.code);
                const st = c?.status ?? "pending";
                return (
                  <li key={i.code} className={st === "error" ? "bad-t" : st === "unknown" ? "up-t" : ""}>
                    {certs && <span className="st">{st === "ok" ? "✅" : st === "error" ? "❌" : st === "unknown" ? "⚠️" : "⏳"}</span>}
                    <code>{i.code}</code> — {rub(i.nominal)}
                    {c?.status === "ok" && c.nominal != null && c.nominal !== i.nominal &&
                      <span className="up-t"> · указали {rub(i.nominal)}, зачислено {rub(c.nominal)}</span>}
                    {(st === "error" || st === "unknown") && c?.error_text && <div className="small">{c.error_text}</div>}
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

type Bad = { row: number; error: string }[];
const badItems = (e: unknown): Bad | null => {
  const d = e instanceof ApiError ? (e.detail as { code?: string; items?: Bad } | null) : null;
  return d?.code === "bad_items" && d.items ? d.items : null;
};

function Wizard({ all, onClose }: { all: Account[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [step, setStep] = useState(1);
  const [picked, setPicked] = useState<number[]>([]);
  const [rows, setRows] = useState<Row[]>(() => [newRow()]);
  const [server, setServer] = useState<Record<number, string>>({});  // ошибки строк от API (422)
  const [ids, setIds] = useState<number[]>([]);  // все записи этого окна — по всем кругам
  const [run, setRun] = useState(false);  // экран активации/итога
  const [again, setAgain] = useState(false);  // круг «← Назад» с неудачными
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const accs = all.filter((a) => picked.includes(a.id));
  useEffect(() => {  // один выбранный — строки сразу на него; снятый с выбора аккаунт — строки без аккаунта
    setRows((rs) => rs.map((r) => ({ ...r, account: picked.length === 1 ? picked[0] : r.account != null && picked.includes(r.account) ? r.account : null })));
  }, [picked.join()]);
  const codes = useMemo(() => [...new Set(rows.map((r) => normCode(r.code)).filter((c): c is string => !!c))].sort(), [rows]);
  const known = useQuery({ queryKey: ["certs-known", codes.join()], enabled: codes.length > 0, staleTime: 30_000,
    queryFn: () => api<Known>("/certificates/known", { method: "POST", body: { codes } }) });
  const problems = useMemo(() => {
    const seen = new Set<string>(), dup = new Set<string>();
    for (const r of rows) { const c = normCode(r.code); if (c) (seen.has(c) ? dup : seen).add(c); }
    return Object.fromEntries(rows.map((r) => [r.key, rowProblem(r, dup, known.data ?? {}, server)]));
  }, [rows, known.data, server]);
  const used = rows.filter((r) => r.code.trim() || r.nominal.trim());
  const complete = used.every((r) => normCode(r.code) && parseNominal(r.nominal) && r.account != null);
  const hasProblems = used.some((r) => problems[r.key]);
  const hint2 = !used.length ? "Добавьте хотя бы один сертификат"
    : !complete ? "У каждой строки нужны код (16 символов), номинал и аккаунт"
    : hasProblems ? "Исправьте строки с ошибками" : "";
  const items: Item[] = used.map((r) => ({ code: normCode(r.code)!, nominal: parseNominal(r.nominal)!, account: r.account! }));
  const total = sum(items.map((i) => i.nominal));

  const certs = useQuery({ queryKey: ["certs", ids.join()], enabled: run && ids.length > 0,
    queryFn: () => api<Cert[]>(`/certificates?${ids.map((i) => `ids=${i}`).join("&")}`),
    refetchInterval: (q) => (q.state.data && q.state.data.every((c) => FINAL.includes(c.status)) ? false : 1500) });
  const list = (certs.data ?? []).filter((c) => ids.includes(c.id));
  const finished = run && list.length === ids.length && list.every((c) => FINAL.includes(c.status));
  useEffect(() => { if (finished) qc.invalidateQueries({ queryKey: ["accounts"] }); }, [finished]);
  const ok = list.filter((c) => c.status === "ok");
  const failed = list.filter((c) => c.status === "error" || c.status === "unknown");

  /** Отправка шага 3. Новый круг: сначала новые/изменённые коды (POST — может вернуть 422 → шаг 2, и тогда в активацию
   *  не ушло ничего), потом retry прежних записей. */
  const submit = async () => {
    const fresh = used.filter((r) => !r.certId || normCode(r.code) !== r.orig);
    const same = used.filter((r) => !fresh.includes(r));
    const back = (bad: Bad, part: Row[]) => {
      setServer(Object.fromEntries(bad.map((x) => [part[x.row].key, x.error])));
      setStep(2);
    };
    const one = (r: Row) => ({ code: normCode(r.code)!, nominal: parseNominal(r.nominal)!, account_id: r.account! });
    setBusy(true); setError("");
    try {
      let next = ids.filter((id) => !fresh.some((r) => r.certId === id));  // старая запись изменённого кода уходит из окна
      if (fresh.length) {
        try {
          next = [...next, ...(await api<{ ids: number[] }>("/certificates", { method: "POST", body: { items: fresh.map(one) } })).ids];
        } catch (e) { const bad = badItems(e); if (bad) return back(bad, fresh); throw e; }
      }
      if (same.length) {
        try {
          await api("/certificates/retry", { method: "POST", body: { items: same.map((r) => ({ id: r.certId, account_id: r.account })) } });
        } catch (e) {
          const bad = badItems(e);
          if (!bad) throw e;
          setIds(next);  // новые коды уже в очереди — их статусы покажем, когда исправят остальное
          return back(bad, same);
        }
      }
      setIds(next); setRun(true); setAgain(false);
      qc.invalidateQueries({ queryKey: ["certs"] });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "ошибка");
    } finally { setBusy(false); }
  };
  const retryAll = async () => {  // «Попробовать ещё раз» — те же аккаунты
    setBusy(true); setError("");
    try {
      await api("/certificates/retry", { method: "POST", body: { items: failed.filter((c) => c.account_id != null)
        .map((c) => ({ id: c.id, account_id: c.account_id })) } });
      await qc.invalidateQueries({ queryKey: ["certs"] });
    } catch (e) { setError(e instanceof ApiError ? e.message : "ошибка"); } finally { setBusy(false); }
  };
  const goBack = () => {  // «← Назад»: мастер только с неудачными, получателей можно сменить
    const accIds = all.map((a) => a.id).filter((id) => failed.some((c) => c.account_id === id) && all.find((a) => a.id === id)!.status === "active");
    setRows(failed.map((c) => newRow({ code: c.code, nominal: String(c.nominal_entered), account: c.account_id, certId: c.id, orig: c.code })));
    setPicked(accIds); setServer({}); setStep(1); setRun(false); setAgain(true); setError("");
  };

  if (run) {
    const shown = list.filter((c) => c.account_id != null).map((c) => ({ code: c.code, nominal: c.nominal_entered, account: c.account_id! }));
    const okSum = rub(sum(ok.map((c) => c.nominal ?? 0)));
    return (
      <>
        <h2>{!finished ? <><span className="spin" /> Активирую… {list.filter((c) => FINAL.includes(c.status)).length} из {ids.length}</>
          : failed.length ? <>Добавлено {ok.length} из {ids.length} · <span className="bad-t">не прошло {failed.length}</span></>
          : <>Добавлено {ok.length} серт. на {okSum} <span className="ok-t">✓</span></>}</h2>
        {finished && failed.length > 0 && <p className="muted small">Зачислено {okSum}. Неудачные можно повторить на тех же аккаунтах
          или вернуться и выбрать другие (поправить код, если в нём опечатка).</p>}
        <Summary accs={all.filter((a) => shown.some((i) => i.account === a.id))} items={shown} certs={list} />
        {finished && <p className="muted small">Операции появятся в истории баланса каждого аккаунта с пометкой «Сертификат».</p>}
        {!finished && <p className="muted small">Можно закрыть окно — активация продолжится. На одном прокси — по одному коду раз в ~3 с.</p>}
        {error && <p className="err">{error}</p>}
        <div className="wiz-foot">
          {finished && failed.length > 0 ? <span className="wiz-btns left">
            <button className="btn ghost" disabled={busy} onClick={goBack}>← Назад</button>
            <button className="btn" disabled={busy || !failed.some((c) => c.account_id != null)} onClick={retryAll}>Попробовать ещё раз</button>
          </span> : <span />}
          <button className={finished && failed.length ? "link" : "btn"} onClick={onClose}>{finished ? "Готово" : "Закрыть"}</button>
        </div>
      </>
    );
  }
  return (
    <>
      <h2>{again ? "Повторить неудачные" : "Добавить сертификаты"}</h2>
      <Steps step={step} />
      {step === 1 && <StepAccounts all={all} picked={picked} setPicked={setPicked} />}
      {step === 2 && <StepCerts accs={accs} rows={rows} problems={problems}
        setRows={(r) => { setRows(r); setServer({}); }} />}
      {step === 3 && <>
        <p className="muted">Проверьте распределение. После подтверждения сертификаты будут активированы на выбранных аккаунтах.</p>
        <Summary accs={accs} items={items} certs={null} />
      </>}
      {error && <p className="err">{error}</p>}
      <div className="wiz-foot">
        <span className={`small ${step === 2 && hint2 ? "up-t" : "muted"}`}>
          {step === 1 ? (picked.length ? `Выбрано аккаунтов: ${picked.length}` : "Выберите хотя бы один аккаунт")
            : step === 2 ? hint2 : <b>Итого: {rub(total)} · {pcs(items.length)}</b>}
        </span>
        <span className="wiz-btns">
          {step === 1 ? <button className="link" onClick={onClose}>Отмена</button>
            : <button className="btn ghost" onClick={() => setStep(step - 1)}>Назад</button>}
          {step === 1 && <button className="btn" disabled={!picked.length} onClick={() => setStep(2)}>Далее</button>}
          {step === 2 && <button className="btn" disabled={!!hint2} onClick={() => setStep(3)}>Проверить</button>}
          {step === 3 && <button className="btn" disabled={busy} onClick={submit}>
            {again ? "Повторить" : "Добавить"} {pcs(items.length)} на {rub(total)}</button>}
        </span>
      </div>
    </>
  );
}

export default function CertModal({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open]);
  if (!open) return null;
  // фон не закрывает: случайный клик мимо стёр бы вписанные коды
  return (
    <div className="modal-back">
      <div className="modal card wide" role="dialog" aria-modal="true" aria-label="Добавить сертификаты">
        <button className="modal-x link" aria-label="Закрыть" onClick={onClose}>✕</button>
        <Wizard all={all} onClose={onClose} />
      </div>
    </div>
  );
}
