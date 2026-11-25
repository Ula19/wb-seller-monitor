import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, time, type Maintenance, type Me, type Status } from "./api";
import Login from "./pages/Login";
import Items from "./pages/Items";
import Sellers from "./pages/Sellers";
import Accounts from "./pages/Accounts";
import Account from "./pages/Account";
import Events from "./pages/Events";
import Targets from "./pages/Targets";
import Proxies from "./pages/Proxies";
import PollSettings from "./pages/PollSettings";
import DevSettings from "./pages/DevSettings";
import Bell from "./Bell";

const NAV = [
  { to: "/items", label: "Товары", icon: "▦" },
  { to: "/targets", label: "Цели", icon: "◎" },
  { to: "/sellers", label: "Продавцы", icon: "★" },
  { to: "/accounts", label: "Аккаунты", icon: "◉" },
  { to: "/proxies", label: "Прокси", icon: "⇄" },
  { to: "/events", label: "События", icon: "≡" },
  { to: "/settings", label: "Настройки", icon: "⚙" },
];
const DEV_NAV = { to: "/dev", label: "Для разработчика", icon: "⚒" };  // только DEV_IDS

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

/** Режим технических работ: всем — баннер с текстом сервера; разработчику — яркая плашка (у него всё работает). */
function MaintBanner({ dev }: { dev: boolean }) {
  const { data: m } = useQuery({ queryKey: ["maintenance"], queryFn: () => api<Maintenance>("/maintenance"), refetchInterval: 30_000 });
  if (!m?.on) return null;
  const low = (t = "") => t.charAt(0).toLowerCase() + t.slice(1);  // «Обновление из WB» → «обновление из WB»
  const closed = m.blocked.map((g) => low(m.groups[g]?.split(":")[0])).join(", ") || "ничего";
  return dev
    ? <div className="maint dev" role="status">🚧 Технические работы включены — закрыто: {closed}
        {m.poll_paused ? " · фоновый опрос WB на паузе" : ""} · бот: выполняется {m.sending}, ждут в очереди {m.pending}
        {" "}· <NavLink to="/dev">выключить</NavLink></div>
    : <div className="maint" role="status">🚧 {m.message}{m.blocked.length ? ` · сейчас недоступно: ${closed}` : ""}</div>;
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
          {(me.data.is_dev ? [...NAV, DEV_NAV] : NAV).map((n) => (
            <NavLink key={n.to} to={n.to} className={n === DEV_NAV ? "nav dev-nav" : "nav"}>
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
          {me.data.is_dev && <NavLink to="/dev" className="dev-ico" aria-label="Для разработчика" title="Для разработчика">⚒</NavLink>}
          <WorkerDot />
          <button className="link" onClick={logout}>Выйти</button>
        </header>
        <MaintBanner dev={me.data.is_dev} />
        <Routes>
          <Route path="/items" element={<Items />} />
          <Route path="/targets" element={<Targets />} />
          <Route path="/sellers" element={<Sellers />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/accounts/:id" element={<Account />} />
          <Route path="/proxies" element={<Proxies />} />
          <Route path="/events" element={<Events />} />
          <Route path="/settings" element={<PollSettings />} />
          <Route path="/dev" element={me.data.is_dev ? <DevSettings /> : <Navigate to="/settings" replace />} />
          <Route path="*" element={<Navigate to="/items" replace />} />
        </Routes>
      </main>
    </div>
  );
}
