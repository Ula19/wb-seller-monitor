import { useState } from "react";

// Сортировка по клику на заголовок: ↑ → ↓ → как было. Пустые значения — всегда внизу. Строки — по алфавиту (ru).
export function useSort<T>(rows: T[], get: Record<string, (r: T) => number | string | null | undefined>) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const g = sort && get[sort.key];
  const sorted = !sort || !g ? rows : [...rows].sort((a, b) => {
    const x = g(a), y = g(b);
    if (x == null) return y == null ? 0 : 1;
    if (y == null) return -1;
    return (typeof x === "string" || typeof y === "string"
      ? String(x).localeCompare(String(y), "ru", { sensitivity: "base" }) : x - y) * sort.dir;
  });
  const th = (key: string, label: string, cls = "num") => (
    <th className={`${cls} sortable`} title="Сортировать"
        onClick={() => setSort((s) => (s?.key !== key ? { key, dir: 1 } : s.dir === 1 ? { key, dir: -1 } : null))}>
      {label}<span className="arrow">{sort?.key === key ? (sort.dir === 1 ? " ↑" : " ↓") : ""}</span>
    </th>
  );
  return { sorted, th };
}
