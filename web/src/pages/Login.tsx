import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Config } from "../api";

// Telegram Login Widget в режиме JS-колбэка: подпись проверяет бэкенд (POST /api/auth/telegram)
export default function Login() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["config"], queryFn: () => api<Config>("/config") });
  const box = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const done = () => qc.invalidateQueries({ queryKey: ["me"] });
  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "сеть недоступна");

  useEffect(() => {
    const bot = cfg.data?.bot_username;
    if (!bot || !box.current) return;
    (window as unknown as { onTelegramAuth: (u: unknown) => void }).onTelegramAuth = (user) =>
      api("/auth/telegram", { method: "POST", body: user }).then(done, fail);
    const s = document.createElement("script");
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.async = true;
    s.setAttribute("data-telegram-login", bot);
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "8");
    s.setAttribute("data-onauth", "onTelegramAuth(user)");
    box.current.replaceChildren(s);
  }, [cfg.data?.bot_username]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    await api("/auth/password", { method: "POST", body: { password } }).then(done, fail);
    setBusy(false);
  };

  return (
    <div className="center">
      <div className="card login">
        <h1>WB Монитор</h1>
        <p className="muted">Цены продавцов Wildberries, аккаунты и заказы.</p>
        {cfg.data?.bot_username ? <div ref={box} className="tg" /> : <p className="muted">Вход через Telegram пока не настроен.</p>}
        {cfg.data?.password_enabled && (
          <form onSubmit={submit} className="pw">
            <input type="password" placeholder="Запасной пароль владельца" value={password}
                   onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
            <button className="btn" disabled={busy || !password}>{busy ? "Проверяю…" : "Войти"}</button>
          </form>
        )}
        {error && <p className="err">{error}</p>}
      </div>
    </div>
  );
}
