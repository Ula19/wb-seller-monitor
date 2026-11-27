// 🏢 Кого показывать у номера: сам номер (розница) и его компании (юрлица/ИП) — галочками. Одно и то же после входа
// («Кого добавить») и во вкладке «Компании» страницы номера. Снять галочку = скрыть: заказы и история остаются.
// «Обновить список» — тот же визит, что «Обновить» баланса (компании берутся в нём третьим запросом).
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Companies } from "../api";
import { MaintNote } from "../accounts";

export function useCompanies(accountId: number | null) {
  return useQuery({
    queryKey: ["companies", accountId], enabled: accountId != null,
    queryFn: () => api<Companies>(`/accounts/${accountId}/companies`),
    refetchInterval: (q) => (q.state.data?.browser_sync ? 3000 : false),  // «Обновить список» идёт — ждём итога
  });
}

export default function CompaniesPick({ accountId, onSaved, intro }: { accountId: number; onSaved?: () => void; intro?: string }) {
  const qc = useQueryClient();
  const q = useCompanies(accountId);
  const [retail, setRetail] = useState(true);
  const [on, setOn] = useState<Set<number>>(new Set());
  const [note, setNote] = useState("");
  const [touched, setTouched] = useState(false);
  useEffect(() => {  // форма — с сервера, пока её не трогали (иначе обновление списка затёрло бы галочки)
    if (!q.data || touched) return;
    setRetail(q.data.retail);
    setOn(new Set(q.data.companies.filter((c) => c.enabled).map((c) => c.id)));
  }, [q.data, touched]);
  const fail = (e: unknown) => setNote(e instanceof ApiError ? e.message : "ошибка");
  const save = useMutation({
    mutationFn: () => api<Companies>(`/accounts/${accountId}/companies`, { method: "PUT", body: { retail, enabled: [...on] } }),
    onSuccess: (d) => {
      qc.setQueryData(["companies", accountId], d);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      setTouched(false); setNote("сохранено"); onSaved?.();
    },
    onError: fail,
  });
  const refresh = useMutation({
    mutationFn: () => api(`/accounts/${accountId}/sync`, { method: "POST" }),
    onSuccess: () => { setNote(""); qc.invalidateQueries({ queryKey: ["companies", accountId] }); },
    onError: fail,
  });
  if (!q.data) return <p className="muted">{q.isError ? "не загрузилось" : "Загрузка…"}</p>;
  const list = q.data.companies;
  const toggle = (id: number) => { setTouched(true); setOn((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; }); };
  return (
    <div className="companies">
      {intro && <p>{intro}</p>}
      <label className="chk"><input type="checkbox" checked={retail} onChange={(e) => { setTouched(true); setRetail(e.target.checked); }} />
        <span><b>Розница</b> <span className="muted small">— сам номер: Кошелёк, оплата, заказы с оплатой сейчас или при получении</span></span></label>
      {list.length ? list.map((c) => (
        <label key={c.id} className="chk"><input type="checkbox" checked={on.has(c.id)} onChange={() => toggle(c.id)} />
          <span><b>{c.name ?? c.full_name ?? `компания ${c.old_id}`}</b>
            <span className="muted small">{c.inn && ` · ИНН ${c.inn}`}{c.kpp && ` · КПП ${c.kpp}`}</span></span></label>
      )) : <p className="muted small">Компаний у номера нет{q.data.browser_sync ? "" : " (или WB их ещё не отдал — «Обновить список»)"}.</p>}
      <MaintNote group="accounts" />
      <div className="row-btns">
        <button className="btn" disabled={save.isPending} onClick={() => save.mutate()}>Сохранить</button>
        <button className="btn ghost" disabled={refresh.isPending || q.data.browser_sync} onClick={() => refresh.mutate()}>
          {q.data.browser_sync ? <><span className="spin" /> Обновляю список…</> : "↻ Обновить список"}</button>
        {note && <span className={note === "сохранено" ? "ok-t small" : "err small"}>{note}</span>}
      </div>
    </div>
  );
}
