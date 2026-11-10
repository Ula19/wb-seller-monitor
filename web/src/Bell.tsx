// 🔔 Уведомления всех аккаунтов WB: бейдж новых (опрос раз в 30 с), панель-лента, «↻ Проверить сейчас».
// Текст WB — только как текст (React экранирует); ссылка — только https://*.wildberries.ru (проверено и на сервере).
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, time, type Notice, type NoticeCheck } from "./api";

const PAGE = 50;
const busy = (c: NoticeCheck | undefined) => c?.state === "requested" || c?.state === "running";
const wbUrl = (u: string | null) => {
  try { const h = new URL(u ?? "").hostname; return u!.startsWith("https://") && (h === "wildberries.ru" || h.endsWith(".wildberries.ru")) ? u : null; }
  catch { return null; }
};

function Row({ n, fresh, onGo }: { n: Notice; fresh: boolean; onGo: () => void }) {
  const [full, setFull] = useState(false);
  const url = wbUrl(n.url);
  return (
    <li className={`ntc ${fresh ? "fresh" : ""}`}>
      <div className="ntc-head">
        <Link className="chip-acc" to={`/accounts/${n.account_id}`} onClick={onGo}>{n.account_title}</Link>
        <span className="muted small">{time(n.dt)}</span>
      </div>
      <b>{n.title ?? "—"}</b>
      {n.text && <div className={`ntc-text small ${full ? "" : "clamp"}`} onClick={() => setFull(!full)}>{n.text}</div>}
      {url && <a className="small" href={url} target="_blank" rel="noreferrer noopener">Открыть на WB ↗</a>}
    </li>
  );
}

function Panel({ check, onClose }: { check: NoticeCheck; onClose: () => void }) {
  const qc = useQueryClient();
  const [pages, setPages] = useState(1);
  const [asked, setAsked] = useState(false);  // итог проверки показываем, только если нажали здесь
  const [err, setErr] = useState("");
  const opened = useRef(Date.now() - 5000);  // подсветка «новых» переживает прочтение, пока панель открыта
  const list = useQuery({ queryKey: ["notices", pages], queryFn: () => api<Notice[]>(`/notices?limit=${pages * PAGE}`) });
  useEffect(() => {  // открыли → через 2 с всё прочитано (как в WB), бейдж 0
    const t = setTimeout(async () => {
      await api("/notices/read", { method: "POST", body: {} }).catch(() => {});
      qc.invalidateQueries({ queryKey: ["notices-unread"] });
    }, 2000);
    return () => clearTimeout(t);
  }, []);
  const was = useRef(busy(check));
  useEffect(() => {  // проверка закончилась — перечитать ленту
    if (was.current && !busy(check)) qc.invalidateQueries({ queryKey: ["notices"] });
    was.current = busy(check);
  }, [check?.state]);
  const start = async () => {
    setErr("");
    try { await api("/notices/check", { method: "POST" }); setAsked(true); }
    catch (e) { if (e instanceof ApiError && e.status === 409) setAsked(true); else setErr(e instanceof ApiError ? e.message : "ошибка"); }
    qc.invalidateQueries({ queryKey: ["notices-unread"] });
  };
  const rows = [...new Map((list.data ?? []).map((n) => [n.id, n])).values()];
  return (
    <>
      <div className="bell-back" onMouseDown={onClose} />
      <div className="bell-panel card" role="dialog" aria-label="Уведомления">
        <div className="bell-top">
          <b>Уведомления</b>
          <button className="btn ghost sm" disabled={busy(check)} onClick={start}>
            {busy(check) ? <><span className="spin" /> Проверяю…</> : "↻ Проверить сейчас"}</button>
          <button className="link modal-x-in" aria-label="Закрыть" onClick={onClose}>✕</button>
        </div>
        {asked && check?.state === "done" && (
          <div className="small bell-res">
            {check.new ? <span className="ok-t">новых: {check.new}</span> : <span className="muted">новых нет</span>}
            {!!check.failed?.length && <div className="up-t">не проверены: {check.failed.join(", ")}</div>}
          </div>
        )}
        {err && <p className="err small">{err}</p>}
        {list.isLoading ? <p className="muted">Загрузка…</p> : !rows.length ? <p className="empty">Уведомлений пока нет</p> : (
          <ul className="ntc-list">
            {rows.map((n) => <Row key={n.id} n={n} onGo={onClose}
              fresh={!n.read_at || Date.parse(n.read_at) >= opened.current} />)}
          </ul>
        )}
        {(list.data?.length ?? 0) >= pages * PAGE && <button className="link" onClick={() => setPages(pages + 1)}>Показать ещё</button>}
      </div>
    </>
  );
}

export default function Bell() {
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["notices-unread"], queryFn: () => api<{ count: number; check: NoticeCheck }>("/notices/unread"),
    refetchInterval: (s) => (busy(s.state.data?.check) ? 2000 : 30_000) });
  const n = q.data?.count ?? 0;
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open]);
  return (
    <>
      <button className="bell" aria-label={n ? `Уведомления: новых ${n}` : "Уведомления"} onClick={() => setOpen(!open)}>
        🔔{n > 0 && <span className="bell-n">{n > 99 ? "99+" : n}</span>}
      </button>
      {/* в body: в меню панель унаследовала бы жирный шрифт «WB Монитор» */}
      {open && createPortal(<Panel check={q.data?.check ?? null} onClose={() => setOpen(false)} />, document.body)}
    </>
  );
}
