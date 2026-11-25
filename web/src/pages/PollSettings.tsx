// Раздел «Настройки» (/settings): как часто worker опрашивает аккаунты — по типам данных, общие для всех; ночной режим.
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Maintenance, type Me } from "../api";

type Poll = { orders: number; balance: number; vb: number; grade: number; notices: number; night: boolean; night_x: number;
  night_from: string; night_to: string };  // "HH:MM" МСК
type Num = Exclude<keyof Poll, "night" | "night_from" | "night_to">;

// [поле, подпись, максимум, запросов к WB за раз] — максимумы как у сервера (repo.POLL_MAX)
const FIELDS: [Num, string, number, number][] = [
  ["orders", "Заказы и статусы", 1440, 2], ["balance", "Баланс и лимит", 1440, 1], ["vb", "ВБ-баллы", 1440, 1],
  ["grade", "% Кошелька", 10080, 1], ["notices", "Уведомления", 1440, 1],
];
const mins = (hhmm: string) => +hhmm.slice(0, 2) * 60 + +hhmm.slice(3, 5);
/** Длина ночи в минутах; конец раньше начала — окно через полночь (23:00–07:00 = 480). */
export const nightLen = (from: string, to: string) => (mins(to) - mins(from) + 1440) % 1440;

/** ≈ запросов в сутки на аккаунт: днём раз в интервал, ночью — в интервал × множитель. */
export function perDay(p: Poll): number {
  const x = p.night ? p.night_x : 1, night = nightLen(p.night_from, p.night_to);
  return FIELDS.reduce((s, [k, , , n]) => s + n * ((1440 - night) / p[k] + night / (p[k] * x)), 0);
}

const bad = (v: number, hi: number) => !Number.isInteger(v) || v < 1 || v > hi;
const hours = (m: number) => m >= 60 && !bad(m, 100000) ? `= ${+(m / 60).toFixed(1)} ч` : "";

function Form({ init }: { init: Poll }) {
  const [p, setP] = useState(init);
  const set = (k: Num) => (e: React.ChangeEvent<HTMLInputElement>) => setP({ ...p, [k]: e.target.value === "" ? NaN : +e.target.value });
  const save = useMutation({ mutationFn: () => api<Poll>("/settings/poll", { method: "PUT", body: p }) });
  useEffect(() => save.reset(), [p]);  // правка после «Сохранено» — снова «Сохранить»
  const badTime = (t: string) => !/^\d{2}:\d{2}$/.test(t);
  const sameTime = p.night_from === p.night_to;
  const wrong = FIELDS.some(([k, , hi]) => bad(p[k], hi)) || bad(p.night_x, 10) || badTime(p.night_from) || badTime(p.night_to) || sameTime;
  const time = (k: "night_from" | "night_to", label: string) => (
    <input type="time" aria-label={label} required value={p[k]} aria-invalid={badTime(p[k]) || sameTime}
      onChange={(e) => setP({ ...p, [k]: e.target.value })} />);
  const field = (k: Num, label: string, hi: number, unit: string, hint = "") => (
    <label className="poll-row" key={k}>
      <span>{label}</span>
      <span className="poll-in"><input type="number" min={1} max={hi} step={1} value={Number.isNaN(p[k]) ? "" : p[k]}
        aria-invalid={bad(p[k], hi)} onChange={set(k)} /> {unit} <span className="muted small">{hint}</span></span>
      {bad(p[k], hi) && <span className="err small">целое от 1 до {hi.toLocaleString("ru-RU")}</span>}
    </label>
  );
  return (
    <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>  {/* неактивная кнопка — и Enter не отправит */}
      <p className="muted small">Как часто бот спрашивает у WB данные каждого аккаунта. Кнопки «↻ Обновить» берут всё сразу.</p>
      <div className="poll-grid">
        {FIELDS.map(([k, label, hi]) => field(k, label, hi, "мин", hours(p[k])))}
        <div className="poll-row">
          <span>История покупок</span><span className="muted small">когда забрали заказ и раз в сутки</span>
        </div>
        <div className="poll-check">
          <input type="checkbox" id="poll-night" checked={p.night} onChange={(e) => setP({ ...p, night: e.target.checked })} />
          <label htmlFor="poll-night">Ночной режим</label>
          <span className="poll-in">с {time("night_from", "Ночь с")} до {time("night_to", "Ночь до")} МСК</span>
        </div>
        {sameTime && <span className="err small">начало и конец совпадают</span>}
        {p.night && field("night_x", "Ночью реже в", 10, "раз")}
      </div>
      <p className="poll-est">{wrong ? "Исправьте поля выше"
        : <>≈ <b>{Math.round(perDay(p)).toLocaleString("ru-RU")}</b> запросов в сутки на аккаунт</>}</p>
      {save.isError && <p className="err">{save.error instanceof ApiError ? save.error.message : "не сохранилось"}</p>}
      <div className="wiz-foot">
        <button className="btn" disabled={wrong || save.isPending || save.isSuccess}>
          {save.isPending ? "Сохраняю…" : save.isSuccess ? "Сохранено ✓" : "Сохранить"}</button>
      </div>
    </form>
  );
}

/** «Общий забор»: кому слать в Telegram — ID через запятую (получатель должен нажать /start у бота). */
export const parseIds = (t: string): number[] | null => {
  const parts = t.split(/[\s,;]+/).filter(Boolean);
  return parts.every((x) => /^\d{1,15}$/.test(x) && +x > 0) && parts.length <= 10 ? [...new Set(parts.map(Number))] : null;
};

function PickupTo() {
  const q = useQuery({ queryKey: ["pickup-to"], queryFn: () => api<{ to: number[] }>("/settings/pickup"), gcTime: 0 });
  const [t, setT] = useState<string | null>(null);
  const text = t ?? q.data?.to.join(", ") ?? "";
  const ids = parseIds(text);
  const save = useMutation({ mutationFn: () => api<{ to: number[] }>("/settings/pickup", { method: "PUT", body: { to: ids } }) });
  useEffect(() => save.reset(), [t]);
  if (!q.data) return q.isError ? <p className="err">Не загрузились настройки</p> : <p className="muted">Загрузка…</p>;
  return (
    <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <label className="poll-row"><span>Кому отправлять заборы в Telegram</span>
        <input aria-label="Telegram ID получателей" placeholder="123456789, 987654321" value={text} aria-invalid={!ids}
          onChange={(e) => setT(e.target.value)} /></label>
      {!ids ? <p className="err small">Только Telegram ID (числа) через запятую, не больше 10</p>
        : <p className="muted small">Получатель должен один раз нажать /start у бота — иначе Telegram не даст ему написать.</p>}
      {save.isError && <p className="err">{save.error instanceof ApiError ? save.error.message : "не сохранилось"}</p>}
      <div className="wiz-foot">
        <button className="btn" disabled={!ids || save.isPending || save.isSuccess}>
          {save.isPending ? "Сохраняю…" : save.isSuccess ? "Сохранено ✓" : "Сохранить"}</button>
      </div>
    </form>
  );
}

/** Режим технических работ — только разработчикам (DEV_IDS). Закрытые группы — для всех остальных 503 + баннер;
 * «деньги» и «забор» ещё и останавливают очередь worker'а. При включении по умолчанию закрыто всё. */
function MaintForm({ m }: { m: Maintenance }) {
  const qc = useQueryClient();
  const all = Object.keys(m.groups);
  const [f, setF] = useState({ on: m.on, text: m.text, until: m.until, poll_paused: m.poll_paused,
    blocked: m.on ? m.blocked : all });
  const save = useMutation({ mutationFn: () => api<Maintenance>("/maintenance", { method: "PUT", body: f }),
    onSuccess: (x) => qc.setQueryData(["maintenance"], x) });
  useEffect(() => save.reset(), [f]);
  const toggle = (g: string) => setF({ ...f, blocked: f.blocked.includes(g) ? f.blocked.filter((x) => x !== g) : [...f.blocked, g] });
  return (
    <form onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <p className="muted small">Закрытое недоступно всем, кроме разработчиков: сервер отвечает текстом баннера. При закрытых
        «деньгах» и «заборе» очередь бота не берёт новые дела — текущее доделает, ждущие пойдут после выключения.</p>
      <div className="poll-check">
        <input type="checkbox" id="maint-on" checked={f.on} onChange={(e) => setF({ ...f, on: e.target.checked })} />
        <label htmlFor="maint-on"><b>Режим технических работ</b></label>
      </div>
      <fieldset className="maint-groups" aria-label="Что закрыть">
        {all.map((g) => <label key={g} className="poll-check"><input type="checkbox" checked={f.blocked.includes(g)}
          onChange={() => toggle(g)} /> {m.groups[g]}</label>)}
        <label className="poll-check"><input type="checkbox" checked={f.poll_paused}
          onChange={(e) => setF({ ...f, poll_paused: e.target.checked })} /> Пауза фонового опроса WB (заказы, баланс, визиты)</label>
      </fieldset>
      <label className="poll-row"><span>Текст для всех</span>
        <input aria-label="Текст баннера" maxLength={200} placeholder="обновляем бота" value={f.text}
          onChange={(e) => setF({ ...f, text: e.target.value })} /></label>
      <label className="poll-row"><span>До, МСК</span>
        <span className="poll-in"><input type="time" aria-label="До" value={f.until} onChange={(e) => setF({ ...f, until: e.target.value })} />
          {f.until && <button type="button" className="link" onClick={() => setF({ ...f, until: "" })}>убрать</button>}</span></label>
      <p className="poll-est">Выполняется сейчас: <b>{m.sending}</b> · ждут в очереди: <b>{m.pending}</b>
        {m.on && <span className="muted small"> — деплой, когда «выполняется» = 0</span>}</p>
      {save.isError && <p className="err">{save.error instanceof ApiError ? save.error.message : "не сохранилось"}</p>}
      <div className="wiz-foot">
        <button className={`btn${f.on && !m.on ? " bad" : ""}`} disabled={save.isPending || save.isSuccess}>
          {save.isPending ? "Сохраняю…" : save.isSuccess ? "Сохранено ✓" : f.on && !m.on ? "Включить режим работ"
            : !f.on && m.on ? "Выключить режим работ" : "Сохранить"}</button>
      </div>
    </form>
  );
}

function MaintCard() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/me") });
  const q = useQuery({ queryKey: ["maintenance"], queryFn: () => api<Maintenance>("/maintenance"),
    refetchInterval: 3000, enabled: !!me.data?.is_dev });
  if (!me.data?.is_dev) return null;
  return (
    <div className="card poll-card">
      <h2>Технические работы</h2>
      {q.data ? <MaintForm key={String(q.data.on)} m={q.data} /> : q.isError ? <p className="err">Не загрузилось</p>
        : <p className="muted">Загрузка…</p>}
    </div>
  );
}

export default function PollSettings() {
  const q = useQuery({ queryKey: ["poll-settings"], queryFn: () => api<Poll>("/settings/poll"), gcTime: 0 });
  return (
    <section>
      <h1>Настройки</h1>
      <MaintCard />
      <div className="card poll-card">
        <h2>Опрос аккаунтов</h2>
        {q.data ? <Form init={q.data} /> : q.isError ? <p className="err">Не загрузились настройки</p> : <p className="muted">Загрузка…</p>}
      </div>
      <div className="card poll-card">
        <h2>Общий забор</h2>
        <PickupTo />
      </div>
    </section>
  );
}
