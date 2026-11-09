// Вход в аккаунт WB — модальным окном (со списка «Аккаунтов» и со страницы аккаунта): номер → код → готово,
// со всеми ветками: отмена, «Отправить ещё раз / по СМС», «номер уже подключён — войти заново?».
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Account, type LoginRequest, type Proxy } from "../api";
import { STATUS } from "../accounts";

const ACTIVE = ["new", "running", "wait_code"];
type Connected = { id: number; title: string; status: string };
export type Relogin = { a: Account; n: number };  // n — чтобы повторный клик по той же кнопке снова сработал

function Connect({ onDone, relogin }: { onDone: () => void; relogin: Relogin | null }) {
  const qc = useQueryClient();
  const [phone, setPhone] = useState("");
  const [already, setAlready] = useState<Connected | null>(null);  // 409: номер уже подключён — спрашиваем
  const [reloginOf, setReloginOf] = useState<string | null>(null);  // «Войти заново» с карточки: без вопроса
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
    mutationFn: (again: boolean) => api("/accounts/login",
      { method: "POST", body: { phone, kind, proxy_id: pid ? Number(pid) : null, relogin: again } }),
    onSuccess: () => { setError(""); setAlready(null); setReloginOf(null); qc.invalidateQueries({ queryKey: ["login"] }); },
    onError: (e: unknown) => {
      const d = e instanceof ApiError ? (e.detail as { code?: string; account?: Connected } | null) : null;
      if (d?.code === "already_connected" && d.account) { setError(""); setAlready(d.account); } else fail(e);
    },
  });
  useEffect(() => {  // «Войти заново» на карточке: номер, тип и прокси этого аккаунта
    if (!relogin) return;
    const { a } = relogin;
    setPhone((a.phone ?? "").replace(/\D/g, "").slice(-10));
    setKind(a.kind);
    setProxyId(!a.proxy ? "" : good.some((p) => p.id === a.proxy!.id) ? String(a.proxy.id) : null);
    setReloginOf(a.title); setAlready(null); setError("");
  }, [relogin?.n]);
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
    <div className="connect">
      <h2>{reloginOf ? `Повторный вход: ${reloginOf}` : "Подключить аккаунт WB"}</h2>
      {!active ? (
        <form className="add" onSubmit={(e) => { e.preventDefault(); start.mutate(!!reloginOf); }}>
          {/* другой номер — уже не «повторный вход» этого аккаунта */}
          <div className="phone"><span>+7</span><input inputMode="tel" placeholder="900 123-45-67" value={phone}
            onChange={(e) => { setPhone(e.target.value); setAlready(null); setReloginOf(null); }} /></div>
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
          {already && <div className="full already">
            <p>Этот номер уже подключён: <b>{already.title}</b> ({STATUS[already.status] ?? already.status}). Войти заново?
              <span className="muted"> Сессия и прокси обновятся, имя и заказы сохранятся.</span></p>
            <button type="button" className="btn sm" disabled={start.isPending} onClick={() => start.mutate(true)}>Войти заново</button>{" "}
            <button type="button" className="link" onClick={() => setAlready(null)}>Отмена</button>
          </div>}
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

export default function LoginModal({ open, onClose, relogin }: { open: boolean; onClose: () => void; relogin: Relogin | null }) {
  const qc = useQueryClient();
  useEffect(() => {  // Esc — закрыть (вход, если идёт, продолжается — откройте окно снова, чтобы ввести код)
    if (!open) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open]);
  if (!open) return null;
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal card" role="dialog" aria-modal="true">
        <button className="modal-x link" aria-label="Закрыть" onClick={onClose}>✕</button>
        <Connect relogin={relogin} onDone={() => qc.invalidateQueries({ queryKey: ["accounts"] })} />
      </div>
    </div>
  );
}
