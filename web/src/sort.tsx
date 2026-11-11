import { useState } from "react";

type Sort = { key: string; dir: 1 | -1 } | null;

/** Сохранённая сортировка: битое значение, неизвестная колонка, недоступное хранилище — без сортировки. */
function load(storageKey: string, keys: string[]): Sort {
  try {
    const s = JSON.parse(localStorage.getItem(`sort.${storageKey}`) ?? "null");
    return s && keys.includes(s.key) && (s.dir === 1 || s.dir === -1) ? { key: s.key, dir: s.dir } : null;
  } catch { return null; }
}

// Сортировка по клику на заголовок: ↑ → ↓ → как было. Пустые значения — всегда внизу. Строки — по алфавиту (ru).
// Выбор помним в браузере (storageKey: accounts / items / targets) — уход со страницы и перезагрузка его не сбрасывают.
export function useSort<T>(rows: T[], get: Record<string, (r: T) => number | string | null | undefined>, storageKey: string) {
  const [sort, setSortState] = useState<Sort>(() => load(storageKey, Object.keys(get)));
  const setSort = (next: Sort) => {
    setSortState(next);
    try { localStorage.setItem(`sort.${storageKey}`, JSON.stringify(next)); } catch { /* приватный режим */ }
  };
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
        onClick={() => setSort(sort?.key !== key ? { key, dir: 1 } : sort.dir === 1 ? { key, dir: -1 } : null)}>
      {label}<span className="arrow">{sort?.key === key ? (sort.dir === 1 ? " ↑" : " ↓") : ""}</span>
    </th>
  );
  return { sorted, th };
}
