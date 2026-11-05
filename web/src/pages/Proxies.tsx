import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, time, type Proxy } from "../api";

export function proxyState(p: Proxy) {
  if (p.checking) return <span className="pill"><span className="spin" /> проверяю…</span>;
  if (p.ok) return <span className="pill ok">✓ работает</span>;
  if (p.ok === false) return <span className="pill bad">✗ не годится</span>;
  return <span className="pill">не проверен</span>;
}

function Row({ p }: { p: Proxy }) {
  const qc = useQueryClient();
  const [err, setErr] = useState("");
  const done = { onSuccess: () => { setErr(""); qc.invalidateQueries({ queryKey: ["proxies"] }); },
    onError: (e: unknown) => setErr(e instanceof ApiError ? e.message : "ошибка") };
  const check = useMutation({ mutationFn: () => api(`/proxies/${p.id}/check`, { method: "POST" }), ...done });
  const del = useMutation({ mutationFn: () => api(`/proxies/${p.id}`, { method: "DELETE" }), ...done });
  return (
    <tr>
      <td><b>{p.label}</b><div className="muted small">{p.server}</div></td>
      <td>{proxyState(p)}{p.error && <div className="err small">{p.error}</div>}</td>
      <td className="num">{p.exit_ip ?? "—"}<div className="muted small">{p.checked_at ? time(p.checked_at) : ""}</div></td>
      <td>{p.used_by.length ? <>занят: {p.used_by.join(", ")}{p.used_by.length > 1 && <div className="up-t small">аккаунты на одном IP</div>}</> : <span className="muted">свободен</span>}</td>
      <td className="num">
        <button className="link" disabled={p.checking || check.isPending} onClick={() => check.mutate()}>Проверить</button>
        {" · "}
        <button className="link danger" disabled={!!p.used_by.length} title={p.used_by.length ? "занятый прокси удалить нельзя" : ""}
          onClick={() => confirm(`Удалить прокси «${p.label}»?`) && del.mutate()}>Удалить</button>
        {err && <div className="err small">{err}</div>}
      </td>
    </tr>
  );
}

export default function Proxies() {
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["proxies"], queryFn: () => api<Proxy[]>("/proxies"),
    refetchInterval: (q) => (q.state.data?.some((p) => p.checking) ? 3000 : 60_000),
  });
  const [label, setLabel] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const add = useMutation({
    mutationFn: () => api("/proxies", { method: "POST", body: { label, url } }),
    onSuccess: () => { setLabel(""); setUrl(""); setError(""); qc.invalidateQueries({ queryKey: ["proxies"] }); },
    onError: (e) => setError(e instanceof ApiError ? e.message : "ошибка"),
  });
  const rows = list.data ?? [];
  return (
    <section>
      <h1>Прокси</h1>
      <p className="lead">
        У каждого аккаунта WB — свой постоянный IP: через него идут вход, обновление сессии, лимит и заказы.
        Не тот, что у мониторинга. После входа прокси аккаунта не меняется — сменить можно только повторным входом.
      </p>
      <form className="card add" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
        <input placeholder="Метка, например «Анна RU»" value={label} onChange={(e) => setLabel(e.target.value)} />
        <input placeholder="http://логин:пароль@хост:порт" value={url} onChange={(e) => setUrl(e.target.value)} autoComplete="off" />
        <button className="btn" disabled={add.isPending || !label.trim() || !url.trim()}>Добавить и проверить</button>
        <p className="full muted small">Пароль хранится зашифрованным и на сайте больше не показывается. socks5 с паролем не подходит — браузер его не умеет.</p>
        {error && <p className="full err">{error}</p>}
      </form>
      <div style={{ height: 16 }} />
      {list.isPending ? <p className="muted">Загрузка…</p> : !rows.length ? (
        <div className="card empty">Прокси пока нет. Без прокси вход в аккаунт идёт через IP мониторинга.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Прокси</th><th>Проверка</th><th className="num">Внешний IP</th><th>Аккаунт</th><th /></tr></thead>
            <tbody>{rows.map((p) => <Row key={p.id} p={p} />)}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}
