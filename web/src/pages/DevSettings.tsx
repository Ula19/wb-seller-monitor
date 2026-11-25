// Раздел «Для разработчика» (/dev): настройки, которые видят и меняют только DEV_IDS. Каждая — своей карточкой:
// новые добавляются сюда же, без переездов. Не-разработчика App отправляет в «Настройки» (сервер и так не даст менять).
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Maintenance } from "../api";

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
  const q = useQuery({ queryKey: ["maintenance"], queryFn: () => api<Maintenance>("/maintenance"), refetchInterval: 3000 });
  return (
    <div className="card poll-card">
      <h2>Технические работы</h2>
      {q.data ? <MaintForm key={String(q.data.on)} m={q.data} /> : q.isError ? <p className="err">Не загрузилось</p>
        : <p className="muted">Загрузка…</p>}
    </div>
  );
}

export default function DevSettings() {
  return (
    <section>
      <h1>Для разработчика</h1>
      <MaintCard />
    </section>
  );
}
