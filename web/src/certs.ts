// Сертификаты WB: код, разбор списка, распределение по аккаунтам — чистые функции мастера «Добавить сертификат».
// Нормализация — та же, что account_api.norm_cert на сервере (он главный: проверяет ещё раз).

// номиналы со страницы покупки WB; «любая сумма от 500 до 100 000 ₽» — вне диапазона только предупреждаем
export const NOMINALS = [500, 1000, 2000, 3000, 5000, 8000, 10000, 15000, 20000, 25000, 30000, 50000, 100000];
export const NOMINAL_MIN = 500, NOMINAL_MAX = 100000;

/** Код в любом виде (дефисы, пробелы, wbcert-, регистр) → 16 символов [A-Z0-9] или null. */
export function normCode(s: string): string | null {
  let c = s.replace(/[\s\-‐-―]/g, "").toUpperCase();
  if (c.length === 22 && c.startsWith("WBCERT")) c = c.slice(6);
  return /^[A-Z0-9]{16}$/.test(c) ? c : null;
}

/** «1 000», «1000 ₽» → 1000; пусто/мусор → null. */
export function parseNominal(s: string): number | null {
  const t = s.replace(/[\s ]/g, "").replace(/(₽|руб\.?|р\.?)$/i, "");
  return /^\d+$/.test(t) && Number(t) > 0 ? Number(t) : null;
}

export type Parsed = { code: string; nominal: number | null };
export type BadLine = { line: string; why: string };

// код — 4 группы по 4 с дефисом/пробелом между ними или без, можно с wbcert-; дальше — номинал
const LINE = /^(?:wbcert-?)?([a-z0-9]{4})[\s\-‐-―]?([a-z0-9]{4})[\s\-‐-―]?([a-z0-9]{4})[\s\-‐-―]?([a-z0-9]{4})(?![a-z0-9])[\s,;:\-–—=]*(.*)$/i;

/** «Вставить списком»: по одному в строке «КОД [номинал]». Плохие строки — с причиной; пустые пропускаем. */
export function parseList(text: string): { rows: Parsed[]; bad: BadLine[] } {
  const rows: Parsed[] = [], bad: BadLine[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(LINE);
    if (!m) { bad.push({ line, why: "не похоже на код — нужно 16 латинских букв и цифр" }); continue; }
    const rest = m[5].trim();
    const nominal = rest ? parseNominal(rest) : null;
    if (rest && nominal == null) { bad.push({ line, why: "номинал — не число" }); continue; }
    rows.push({ code: (m[1] + m[2] + m[3] + m[4]).toUpperCase(), nominal });
  }
  return { rows, bad };
}

/** «Распределить поровну»: по кругу в порядке выбранных аккаунтов; не делится — лишние первым (5 на 2 → 3 + 2). */
export function distribute<T>(rows: T[], accounts: number[]): (T & { account: number | null })[] {
  return rows.map((r, i) => ({ ...r, account: accounts.length ? accounts[i % accounts.length] : null }));
}

const fmt = (n: number) => `${n.toLocaleString("ru-RU")} ₽`;

/** Сколько можно пополнить в пределах лимита скидки: L − W (неоплаченные не учитываем — слово владельца); null — лимит неизвестен. */
export const canTopUp = (L: number | null, W: number) => (L == null ? null : Math.max(0, L - W));

/** Текст подтверждения пополнения сверх лимита; никто не сверх — null (без подтверждения). */
export function overConfirm(rows: { title: string; n: number; can: number | null }[]): string | null {
  const over = rows.filter((r) => r.can != null && r.n > r.can);
  return over.length ? `Сверх лимита скидки:\n\n${over.map((r) => `${r.title}: ${fmt(r.n)}, можно ${fmt(r.can!)} (сверх на ${fmt(r.n - r.can!)})`).join("\n")}\n\nВсё равно пополнить?` : null;
}

/** Оплата больше остатка лимита — с flags 0 WB, скорее всего, откажет (не спишет полную цену). */
export const payOverLimit = (sum: number, L: number | null) => (L != null && sum > L
  ? `Сумма больше остатка лимита ${fmt(L)} — WB, скорее всего, откажет в оплате. Оплатите частями, чтобы уложиться в лимит` : null);

export function selfCheck() {
  const eq = (a: unknown, b: unknown) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${JSON.stringify(a)} != ${JSON.stringify(b)}`); };
  for (const s of ["UYPMJUAFGZEVR89S", "uypm-juaf-gzev-r89s", "wbcert-UYPM-JUAF-GZEV-R89S", " WBCERT-uypmjuafgzevr89s ", "UYPM JUAF GZEV R89S"])
    eq(normCode(s), "UYPMJUAFGZEVR89S");
  for (const s of ["", "UYPMJUAFGZEVR89", "UYPMJUAFGZEVR89SS", "UYPM_JUAF_GZEV_R89S", "wbcert-UYPM"]) eq(normCode(s), null);
  eq(normCode("WBCERTAAAAAAAAAA"), "WBCERTAAAAAAAAAA");
  eq([parseNominal("1 000"), parseNominal("1000 ₽"), parseNominal("500р"), parseNominal("0"), parseNominal("abc")], [1000, 1000, 500, null, null]);
  const { rows, bad } = parseList([
    "UYPMJUAFGZEVR89S 1000", "uypm-juaf-gzev-r89a", "wbcert-UYPM-JUAF-GZEV-R89B 5 000 ₽", "UYPM JUAF GZEV R89C\t2000",
    "  ", "UYPMJUAFGZEVR89D — 500", "UYPMJUAFGZEVR89E;80", "привет", "UYPMJUAFGZEVR89 1000", "UYPMJUAFGZEVR89SX 1000",
    "UYPMJUAFGZEVR89F много",
  ].join("\n"));
  eq(rows, [{ code: "UYPMJUAFGZEVR89S", nominal: 1000 }, { code: "UYPMJUAFGZEVR89A", nominal: null },
    { code: "UYPMJUAFGZEVR89B", nominal: 5000 }, { code: "UYPMJUAFGZEVR89C", nominal: 2000 },
    { code: "UYPMJUAFGZEVR89D", nominal: 500 }, { code: "UYPMJUAFGZEVR89E", nominal: 80 }]);
  eq(bad.map((b) => b.line), ["привет", "UYPMJUAFGZEVR89 1000", "UYPMJUAFGZEVR89SX 1000", "UYPMJUAFGZEVR89F много"]);
  eq(bad[3].why, "номинал — не число");
  const five = distribute([1, 2, 3, 4, 5].map((n) => ({ n })), [10, 20]).map((r) => r.account);
  eq(five, [10, 20, 10, 20, 10]);  // 3 + 2
  eq(distribute([{ n: 1 }], [10, 20, 30]).map((r) => r.account), [10]);  // 1 на 3 — только первому
  eq(distribute([{ n: 1 }], []).map((r) => r.account), [null]);
  eq([canTopUp(10000, 5000), canTopUp(10000, 12000), canTopUp(null, 5), canTopUp(0, 0)], [5000, 0, null, 0]);
  eq(overConfirm([{ title: "Таня", n: 5000, can: 5000 }, { title: "Ира", n: 9, can: null }]), null);  // ровно в лимит и неизвестный — молча
  const t = overConfirm([{ title: "Аня", n: 50000, can: 5000 }, { title: "Таня", n: 1, can: 5 }]) ?? "";
  eq([t.includes("Аня: 50"), t.includes("можно 5"), t.includes("(сверх на 45"), t.includes("Таня"), t.endsWith("Всё равно пополнить?")], [true, true, true, false, true]);
  eq([payOverLimit(101, 100) != null, payOverLimit(100, 100), payOverLimit(5, null)], [true, null, null]);
  return "ok";
}
