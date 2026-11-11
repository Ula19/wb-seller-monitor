// «История сертификатов» (меню «Функции»): все добавленные коды — когда (МСК), куда, номинал, чем кончилось.
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, rub, type Account, type Cert } from "../api";
import { CertCode, Modal, Period, usePeriod, when } from "../accounts";

const ST: Record<Cert["status"], string> = { ok: "активирован", error: "ошибка", unknown: "проверьте баланс", pending: "ждёт", sending: "ждёт" };

function History({ all }: { all: Account[] }) {
  const period = usePeriod(1);  // по умолчанию — сегодня
  const { from, to } = period;
  const [acc, setAcc] = useState("");
  const [q, setQ] = useState("");
  const data = useQuery({ queryKey: ["cert-history", from, to, acc],
    queryFn: () => api<Cert[]>(`/certificates/history?${new URLSearchParams({ ...(from && { from }), ...(to && { to }), ...(acc && { account_id: acc }) })}`) });
  const s = q.trim().toLowerCase(), code = s.replace(/[\s-]/g, "").toUpperCase();
  const rows = (data.data ?? []).filter((c) => !s || (code && c.code.includes(code)) || c.account_title?.toLowerCase().includes(s));
  const ok = rows.filter((c) => c.status === "ok");
  return (
    <>
      <h2>История сертификатов</h2>
      <div className="filters">
        <Period p={period} presets={[1, 7, 30, null]} />
        <select aria-label="Аккаунт" value={acc} onChange={(e) => setAcc(e.target.value)}>
          <option value="">Все аккаунты</option>
          {all.map((a) => <option key={a.id} value={a.id}>{a.title}</option>)}
        </select>
        <input className="search" placeholder="Код или аккаунт" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <p className="sum">Активировано: <b>{ok.length} шт.</b> на <b>{rub(ok.reduce((x, c) => x + (c.nominal ?? 0), 0))}</b></p>
      {data.isLoading ? <p className="muted">Загрузка…</p> : !rows.length ? <p className="empty">За этот период сертификатов нет</p> : (
        <div className="table-wrap">
          <table className="cert-hist">
            <thead><tr><th>Дата</th><th>Код</th><th>Аккаунт</th><th className="num">Номинал</th><th>Статус</th></tr></thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id}>
                  <td className="nowrap small">{when(c.created_at)}</td>
                  <td><CertCode code={c.code} /></td>
                  <td>{c.account_title ?? <span className="muted">удалён</span>}</td>
                  <td className="num">{c.nominal != null ? rub(c.nominal) : <span className="muted">{rub(c.nominal_entered)}</span>}</td>
                  <td className={c.status === "ok" ? "ok-t" : c.status === "error" ? "bad-t" : c.status === "unknown" ? "up-t" : "muted"}>
                    {ST[c.status]}{c.error_text && c.status !== "ok" && <div className="small">{c.error_text}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function CertHistory({ open, onClose, all }: { open: boolean; onClose: () => void; all: Account[] }) {
  if (!open) return null;
  return <Modal label="История сертификатов" onClose={onClose} wide backClose><History all={all} /></Modal>;
}
