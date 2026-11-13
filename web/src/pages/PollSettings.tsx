// «Настройки» (меню «Функции»): как часто worker опрашивает аккаунты — по типам данных, общие для всех; ночной режим.
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "../api";
import { Modal } from "../accounts";

type Poll = { orders: number; balance: number; vb: number; grade: number; notices: number; night: boolean; night_x: number };
type Num = Exclude<keyof Poll, "night">;

// [поле, подпись, максимум, запросов к WB за раз] — максимумы как у сервера (repo.POLL_MAX)
const FIELDS: [Num, string, number, number][] = [
  ["orders", "Заказы и статусы", 1440, 2], ["balance", "Баланс и лимит", 1440, 1], ["vb", "ВБ-баллы", 1440, 1],
  ["grade", "% Кошелька", 10080, 1], ["notices", "Уведомления", 1440, 1],
];
const NIGHT_MIN = 6 * 60;  // 01:00–07:00 МСК

/** ≈ запросов в сутки на аккаунт: днём раз в интервал, ночью — в интервал × множитель. */
export function perDay(p: Poll): number {
  const x = p.night ? p.night_x : 1;
  return FIELDS.reduce((s, [k, , , n]) => s + n * ((1440 - NIGHT_MIN) / p[k] + NIGHT_MIN / (p[k] * x)), 0);
}

const bad = (v: number, hi: number) => !Number.isInteger(v) || v < 1 || v > hi;
const hours = (m: number) => m >= 60 && !bad(m, 100000) ? `= ${+(m / 60).toFixed(1)} ч` : "";

function Form({ init }: { init: Poll }) {
  const [p, setP] = useState(init);
  const set = (k: Num) => (e: React.ChangeEvent<HTMLInputElement>) => setP({ ...p, [k]: e.target.value === "" ? NaN : +e.target.value });
  const save = useMutation({ mutationFn: () => api<Poll>("/settings/poll", { method: "PUT", body: p }) });
  useEffect(() => save.reset(), [p]);  // правка после «Сохранено» — снова «Сохранить»
  const wrong = FIELDS.some(([k, , hi]) => bad(p[k], hi)) || bad(p.night_x, 10);
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
        <label className="poll-check">
          <input type="checkbox" checked={p.night} onChange={(e) => setP({ ...p, night: e.target.checked })} />
          Ночной режим 01:00–07:00 МСК — интервалы реже
        </label>
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

export default function PollSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const q = useQuery({ queryKey: ["poll-settings"], queryFn: () => api<Poll>("/settings/poll"), enabled: open, gcTime: 0 });
  if (!open) return null;
  return (
    <Modal label="Настройки" onClose={onClose}>
      <h2>Настройки опроса</h2>
      {q.data ? <Form init={q.data} /> : q.isError ? <p className="err">Не загрузились настройки</p> : <p className="muted">Загрузка…</p>}
    </Modal>
  );
}
