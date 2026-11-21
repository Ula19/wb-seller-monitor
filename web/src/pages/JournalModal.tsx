// «Журнал действий»: заказали, отменили, оплатили, сертификаты, заборы — все аккаунты на одном экране, из БД сайта.
// «Обновить» — флаг для worker'а: заказы всех аккаунтов (пропавшие сами догрузят архив), прогресс «обновляю N из M».
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, MSK, type Account, type Journal, type JournalType } from "../api";
import { Modal, Period, usePeriod } from "../accounts";

const TABS: [JournalType | "all", string][] = [["all", "Все"], ["ordered", "Заказали"], ["cancelled", "Отменили"],
  ["paid", "Оплатили"], ["cert", "Сертификаты"], ["pickup", "Заборы"]];
const LABEL: Record<JournalType, [string, string]> = {  // [ярлык, цвет pill]
  ordered: ["Заказали", "g3"], cancelled: ["Отменили", "bad"], paid: ["Оплатили", "ok"], cert: ["Сертификат", "ok"], pickup: ["Забор", ""],
};
const ddmm = (iso: string) => new Date(iso).toLocaleDateString("ru-RU", { timeZone: MSK, day: "2-digit", month: "2-digit" });
const hhmm = (iso: string) => new Date(iso).toLocaleTimeString("ru-RU", { timeZone: MSK, hour: "2-digit", minute: "2-digit" });
const going = (j?: Journal) => j?.check?.state === "requested" || j?.check?.state === "running";

function Body({ all, onClose }: { all: Account[]; onClose: () => void }) {
  const period = usePeriod(1);  // по умолчанию — сегодня
  const { from, to } = period;
  const [acc, setAcc] = useState("");
  const [tab, setTab] = useState<JournalType | "all">("all");
  const [err, setErr] = useState("");
  const q = useQuery({ queryKey: ["journal", from, to, acc],
    queryFn: () => api<Journal>(`/journal?${new URLSearchParams({ ...(from && { from }), ...(to && { to }), ...(acc && { account_id: acc }) })}`),
    refetchInterval: (x) => (going(x.state.data) ? 1500 : false) });
  const j = q.data;
  const refresh = async () => {
    setErr("");
    try { await api("/journal/refresh", { method: "POST" }); await q.refetch(); }
    catch (e) { setErr(e instanceof ApiError ? e.message : "ошибка"); }
  };
  const rows = (j?.events ?? []).filter((e) => tab === "all" || e.type === tab);
  const ch = j?.check;
  return (
    <>
      <h2>Журнал действий</h2>
      <div className="filters">
        <Period p={period} presets={[1, 7, 30, null]} />
        <select aria-label="Аккаунт" value={acc} onChange={(e) => setAcc(e.target.value)}>
          <option value="">Все аккаунты</option>
          {all.map((a) => <option key={a.id} value={a.id}>{a.title}</option>)}
        </select>
        <span className="journal-upd small muted">
          {going(j) ? <><span className="spin" /> обновляю {ch?.done ?? 0} из {ch?.total ?? "…"}</>
            : ch?.at && ch.state === "done" ? `Данные на ${hhmm(ch.at)}${mskToday(ch.at) ? "" : `, ${ddmm(ch.at)}`}` : ""}
          {" "}<button className="btn ghost sm" disabled={going(j)} onClick={refresh}>↻ Обновить</button>
        </span>
      </div>
      {ch?.state === "done" && !!ch.failed?.length && <p className="small up-t">Не обновлены: {ch.failed.join(", ")}</p>}
      {err && <p className="err">{err}</p>}
      <div className="seg journal-tabs" role="tablist">
        {TABS.map(([t, label]) => <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""}
          onClick={() => setTab(t)}>{label} {j?.counts[t] ?? 0}</button>)}
      </div>
      {q.isLoading ? <p className="muted">Загрузка…</p> : !rows.length ? <p className="empty">За этот период событий нет</p> : (
        <div className="table-wrap journal">
          <table>
            <thead><tr><th>Дата</th><th>Действие</th><th>Аккаунт</th><th>Подробности</th></tr></thead>
            <tbody>
              {rows.map((e, i) => (
                <tr key={`${e.type}${e.at}${i}`}>
                  <td className="nowrap">{ddmm(e.at)}<div className="small muted">{hhmm(e.at)}</div></td>
                  <td><span className={`pill ${LABEL[e.type][1]}`}>{LABEL[e.type][0]}</span></td>
                  <td>{e.account_title}</td>
                  <td>{e.text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="wiz-foot">
        <span className="small muted">Событий: {rows.length}</span>
        <button className="btn" onClick={onClose}>Закрыть</button>
      </div>
    </>
  );
}

const mskToday = (iso: string) => ddmm(iso) === ddmm(new Date().toISOString());

export default function JournalModal({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  if (!open) return null;
  return <Modal label="Журнал действий" onClose={onClose} wide backClose><Body all={all} onClose={onClose} /></Modal>;
}
