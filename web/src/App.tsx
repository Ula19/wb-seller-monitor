import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, time, type Me, type Status } from "./api";
import Login from "./pages/Login";
import Items from "./pages/Items";
import Sellers from "./pages/Sellers";
import Accounts from "./pages/Accounts";
import Account from "./pages/Account";
import Events from "./pages/Events";
import Targets from "./pages/Targets";
import Proxies from "./pages/Proxies";
import Bell from "./Bell";

const NAV = [
  { to: "/items", label: "Товары", icon: "▦" },
  { to: "/targets", label: "Цели", icon: "◎" },
  { to: "/sellers", label: "Продавцы", icon: "★" },
  { to: "/accounts", label: "Аккаунты", icon: "◉" },
  { to: "/proxies", label: "Прокси", icon: "⇄" },
  { to: "/events", label: "События", icon: "≡" },
];

function WorkerStatus() {
  const { data } = useQuery({ queryKey: ["status"], queryFn: () => api<Status>("/status"), refetchInterval: 30_000 });
  if (!data) return null;
  const p = data.last_pass;
  return (
    <div className="worker">
      <div><span className={`dot ${data.worker_alive ? "ok" : "bad"}`} /> мониторинг {data.worker_alive ? "работает" : "не отвечает"}</div>
      {p && <div className="muted">{p.paused ? "пауза: вне рабочих часов" : `цикл ${p.took} с`} · {time(p.at)}</div>}
      <div className="muted">{data.sellers} продавцов · {data.items} товаров</div>
    </div>
  );
}

function WorkerDot() {  // узкий экран: только точка-индикатор, подробности — на ПК в меню
  const { data } = useQuery({ queryKey: ["status"], queryFn: () => api<Status>("/status"), refetchInterval: 30_000 });
  return data ? <span className="muted small"><span className={`dot ${data.worker_alive ? "ok" : "bad"}`} />{data.worker_alive ? "работает" : "не отвечает"}</span> : null;
}

export default function App() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/me"), retry: false });
  if (me.isPending) return <div className="center muted">Загрузка…</div>;
  if (me.isError) return <Login />;

  const logout = async () => {
    await api("/logout", { method: "POST" });
    qc.clear();
  };
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">WB Монитор <Bell /></div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className="nav">
              <span className="ico">{n.icon}</span>
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <WorkerStatus />
          <button className="link" onClick={logout}>Выйти{me.data.username ? ` (@${me.data.username})` : ""}</button>
        </div>
      </aside>
      <main className="main">
        <header className="mob">
          <b>WB Монитор</b>
          <Bell />
          <WorkerDot />
          <button className="link" onClick={logout}>Выйти</button>
        </header>
        <Routes>
          <Route path="/items" element={<Items />} />
          <Route path="/targets" element={<Targets />} />
          <Route path="/sellers" element={<Sellers />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/accounts/:id" element={<Account />} />
          <Route path="/proxies" element={<Proxies />} />
          <Route path="/events" element={<Events />} />
          <Route path="*" element={<Navigate to="/items" replace />} />
        </Routes>
      </main>
    </div>
  );
}
