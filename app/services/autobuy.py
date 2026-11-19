"""«Заказать товар» на аккаунты, worker (SCOUT 2026-11-19 «🛒 Заказать товар», часть 3).

api в WB не ходит, поэтому всё, что требует WB, считает worker:
- order_quotes — запросы сайта: карточка товара по артикулу/ссылке (product) и предпросмотр (preview: цена каждой
  строки на её ПВЗ, Кошелёк, лимит скидки, остаток постоплаты). Свой цикл, раз в секунду, параллельно.
- autobuy_orders — заказы и отмены: очередь прокси в keeper._cert_lane (сертификаты → оплаты → отмены → заказы).
Перед отправкой цена (detail с dest ПВЗ), grade и баланс берутся ЗАНОВО: grade подписан WB и привязан ко времени.
Деньги — в копейках.
"""
import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.config import settings
from app.crypto import decrypt_json
from app.db import models
from app.db.base import Session
from app.services import keeper
from app.wb import account_api, checkout
from app.wb.account_api import NoBearer, Unauthorized
from app.wb.web_client import CATALOG_PARAMS, DETAIL_URL, web_client
from app.wb.web_session import ProxyDown

POSTPAID_WINDOW = timedelta(minutes=20)  # grade пересчитывает остаток постоплаты с задержкой ~15 мин — вычитаем свои
QUOTE_POLL_SEC = 1
LOGIN = "нужен вход в аккаунт — войдите заново"
PROXY = "прокси не отвечает — проверьте его в разделе «Прокси»"
NO_CARD = "WB не отдал карточку товара — попробуйте ещё раз"
NOT_SENT = " — заказ не отправляли"

log = logging.getLogger(__name__)
A, Q, PP = models.AutobuyOrder, models.OrderQuote, models.PickupPoint
_tasks: set = set()  # чтобы задачи запросов не собрал сборщик мусора


class QuoteError(Exception):
    """Понятная человеку причина, почему запрос сайта не выполнить."""


class NoCard(Exception):
    """detail не пришёл (антибот / сеть) — не «товара нет»."""


def rub(kop: int | None) -> str:
    """Копейки → «1 234 ₽» (с копейками, только если они есть)."""
    if kop is None:
        return "? ₽"
    s = f"{kop / 100:,.2f}" if kop % 100 else f"{kop // 100:,}"
    return s.replace(",", " ") + " ₽"


async def fetch_product(nm: int, dest: int) -> dict | None:
    """detail товара с dest ПВЗ (цена и склады зависят от региона). None — товара нет; NoCard — WB не ответил."""
    base = {k: v for k, v in CATALOG_PARAMS.items() if k != "sort"}
    items = await web_client._get_items("detail", DETAIL_URL, {**base, "dest": dest, "nm": nm})
    if items is None:
        raise NoCard(nm)
    return next((x for x in items if x.get("id") == nm and x.get("sizes")), None)


def stock(p: dict) -> int:
    """Сколько можно заказать: остатки первого размера (его и заказываем — у техники размеров нет)."""
    return sum(s.get("qty") or 0 for s in p["sizes"][0].get("stocks") or [])


def card(p: dict) -> dict:
    try:
        price = checkout.prices(p, 0)[0]
    except ValueError:  # нет в наличии — WB не отдаёт цену
        price = None
    left = stock(p)
    return {"nm": p["id"], "name": p.get("name"), "brand": p.get("brand"),
            "color": ((p.get("colors") or [{}])[0]).get("name"), "price": price, "stock": left,
            "in_stock": bool(left and price)}


async def quote_product(req: dict) -> dict:
    nm = checkout.parse_nm(req.get("q"))
    if not nm:
        raise QuoteError("не похоже на артикул или ссылку WB")
    p = await fetch_product(nm, settings.wb_web_dest)
    if not p:
        raise QuoteError(f"товар {nm} не найден")
    return card(p)


def _line(ln: dict, pvzs: dict, got: dict, wallet_pct: int) -> dict:
    """Строка предпросмотра: цена за штуку на ЭТОТ ПВЗ (total и со скидкой Кошелька) или ошибка."""
    out = {"nm": ln["nm"], "qty": ln["qty"], "pvz_id": ln["pvz_id"]}
    pvz = pvzs.get(ln["pvz_id"])
    if not pvz:
        return {**out, "error": "ПВЗ закрыт или не найден"}
    p = got.get((ln["nm"], pvz.dest))
    if isinstance(p, BaseException):
        return {**out, "error": NO_CARD}
    if not p:
        return {**out, "error": "товар не найден"}
    try:
        total, final = checkout.prices(p, wallet_pct)
    except ValueError:
        return {**out, "name": p.get("name"), "error": "нет в наличии"}
    return {**out, "name": p.get("name"), "total": total, "final": final, "stock": stock(p),
            **({} if stock(p) else {"error": "нет в наличии"})}


def _kop(bal: dict, key: str) -> int | None:
    return None if bal.get(key) is None else bal[key] * 100  # parse_balance отдаёт ₽


def discount_lost(order_final: int, unit_final: int, bal: dict, left: int | None = None) -> str | None:
    """Почему WB снимет скидку Кошелька со ВСЕГО заказа (память wb-wallet-discount, корзина 04.11; orders_pay): сумма
    больше остатка месячного лимита, больше разового лимита заказа, позиция дороже лимита позиции. None — скидка будет.
    left — остаток лимита с учётом заказов перед этим (в предпросмотре), иначе из баланса."""
    left = _kop(bal, "limit_left") if left is None else left
    if left is not None and order_final > left:
        return f"остаток лимита {rub(left)}"
    if (cap := _kop(bal, "max_order")) is not None and order_final > cap:
        return f"заказ больше {rub(cap)}"
    if (cap := _kop(bal, "max_rid")) is not None and unit_final > cap:
        return f"товар дороже {rub(cap)}"
    return None


def _pay_now_sums(rows: list, bal: dict) -> list[str]:
    """Каждой строке (= отдельный заказ, по порядку) — now (к оплате сейчас) и no_discount (причина или None);
    лимит месяца расходуется заказами по очереди. → причины без повторов."""
    left, reasons = _kop(bal, "limit_left"), []
    for row in rows:
        if "final" not in row:
            continue
        order_final = row["final"] * row["qty"]
        row["no_discount"] = why = discount_lost(order_final, row["final"], bal, left)
        row["now"] = row["total"] * row["qty"] if why else order_final
        if why:
            reasons.append(why)
        elif left is not None:
            left -= order_final
    return list(dict.fromkeys(reasons))


async def _fresh_account(acc: models.WbAccount) -> tuple[dict, dict]:
    """Свежие grade и full-balance аккаунта через его прокси. QuoteError — с понятной причиной."""
    state, proxy = decrypt_json(acc.state_enc), await keeper.proxy_url(acc.proxy_id)
    try:
        return await asyncio.gather(checkout.fetch_grade(state, proxy), account_api.fetch_balance(state, proxy))
    except (Unauthorized, NoBearer):
        raise QuoteError(LOGIN)
    except ProxyDown:
        raise QuoteError(PROXY)
    except Exception as e:
        log.warning("autobuy: grade/баланс %s не получены: %s: %s", acc.title, type(e).__name__, str(e)[:200])
        raise QuoteError("WB не ответил — попробуйте ещё раз")


async def _preview_account(aid: int, acc, lines: list, pvzs: dict, got: dict, recent: int) -> dict:
    """По аккаунту: деньги, лимиты и строки. now_sum — к оплате сейчас: у заказа без скидки Кошелька (discount_lost)
    — total, а не final."""
    if acc is None:
        return {"account_id": aid, "error": "нет такого аккаунта"}
    try:
        grade, bal = await _fresh_account(acc)
    except QuoteError as e:
        return {"account_id": aid, "title": acc.title, "error": str(e)}
    g, w = grade.get("payload") or {}, account_api.parse_grade(grade) or 0
    rows = [_line(ln, pvzs, got, w) for ln in lines]
    total = sum(r["total"] * r["qty"] for r in rows if "total" in r)
    final = sum(r["final"] * r["qty"] for r in rows if "final" in r)
    funds, limit_left = _kop(bal, "funds"), _kop(bal, "limit_left")
    reasons = _pay_now_sums(rows, bal)
    now_sum = sum(r["now"] for r in rows if "now" in r)
    postpaid_left = (g.get("remaining_postpaid_limit") or 0) - recent
    prepaid_only = bool((g.get("fraud") or {}).get("is_prepaid"))
    return {"account_id": aid, "title": acc.title, "wallet_pct": w, "funds": funds, "limit_left": limit_left,
            "wallet_blocked": bal.get("wallet_blocked"), "postpaid_limit": g.get("postpaid_limit"),
            "postpaid_left": postpaid_left, "prepaid_only": prepaid_only, "lines": rows, "total": total,
            "final": final, "now_sum": now_sum, "discount_over": bool(reasons),
            "note": "скидки не будет: " + "; ".join(reasons) if reasons else None,
            "can_postpay": not prepaid_only and postpaid_left >= total,
            "wallet_enough": not bal.get("wallet_blocked") and funds is not None and funds >= now_sum}


async def _recent_postpaid(s, account_ids) -> dict[int, int]:
    """Σ total × qty наших заказов «при получении» за POSTPAID_WINDOW (grade их ещё не вычел)."""
    rows = await s.execute(select(A.account_id, func.sum(func.coalesce(A.total, A.price_ok) * A.qty)).where(
        A.account_id.in_(account_ids), A.pay_now.is_(False), A.status.in_(("sending", "ok", "unknown")),
        A.created_at >= datetime.now(timezone.utc) - POSTPAID_WINDOW).group_by(A.account_id))
    return {a: int(v) for a, v in rows.all()}


async def quote_preview(user_id: int, req: dict) -> dict:
    """lines: [{account_id, nm, qty, pvz_id}] → по аккаунтам + где товара на складах меньше, чем Σ qty по всем."""
    lines = req.get("lines") or []
    aids = list(dict.fromkeys(ln["account_id"] for ln in lines))
    async with Session() as s:
        accs = {a.id: a for a in await s.scalars(select(models.WbAccount).where(
            models.WbAccount.id.in_(aids), models.WbAccount.user_id == user_id))}
        pvzs = {p.id: p for p in await s.scalars(select(PP).where(
            PP.id.in_({ln["pvz_id"] for ln in lines}), PP.closed_at.is_(None)))}
        recent = await _recent_postpaid(s, list(accs))
    keys = list({(ln["nm"], pvzs[ln["pvz_id"]].dest) for ln in lines if ln["pvz_id"] in pvzs})
    got = dict(zip(keys, await asyncio.gather(*(fetch_product(nm, dest) for nm, dest in keys), return_exceptions=True)))
    accounts = await asyncio.gather(*(
        _preview_account(aid, accs.get(aid), [ln for ln in lines if ln["account_id"] == aid], pvzs, got, recent.get(aid, 0))
        for aid in aids))
    need, have = Counter(), {}
    for ln in lines:
        need[ln["nm"]] += ln["qty"]
    for (nm, _), p in got.items():
        if isinstance(p, dict):
            have[nm] = stock(p)
    over = [{"nm": nm, "need": n, "stock": have[nm]} for nm, n in need.items() if nm in have and n > have[nm]]
    return {"accounts": list(accounts), "stock_over": over}


async def _run_quote(qid: int) -> None:
    async with Session() as s:
        q = await s.get(Q, qid)
        kind, req, user_id = q.kind, q.request, q.user_id
    values = {"status": "error"}
    try:
        result = await (quote_product(req) if kind == "product" else quote_preview(user_id, req))
        values = {"status": "done", "result": result}
    except QuoteError as e:
        values["error"] = str(e)[:256]
    except NoCard:
        values["error"] = NO_CARD
    except Exception as e:
        log.exception("autobuy: запрос %s (%s)", qid, kind)
        values["error"] = f"ошибка: {type(e).__name__}"
    async with Session() as s:
        await s.execute(update(Q).where(Q.id == qid).values(done_at=datetime.now(timezone.utc), **values))
        await s.commit()


async def quotes_loop() -> None:
    """Фоновая задача worker'а: pending → working → каждая в своей задаче. При старте working → error."""
    async with Session() as s:
        await s.execute(update(Q).where(Q.status.in_(("pending", "working"))).values(
            status="error", error="прервано перезапуском — попробуйте ещё раз", done_at=datetime.now(timezone.utc)))
        await s.commit()
    while True:
        try:
            async with Session() as s:
                ids = list(await s.scalars(update(Q).where(Q.status == "pending").values(status="working").returning(Q.id)))
                await s.commit()
            for qid in ids:
                t = asyncio.create_task(_run_quote(qid))
                _tasks.add(t)
                t.add_done_callback(_tasks.discard)
        except Exception:
            log.exception("autobuy: цикл запросов")
        await asyncio.sleep(QUOTE_POLL_SEC)


# ---------- заказ и отмена (очередь прокси, keeper._cert_lane) ----------
async def _finish(oid: int, **values) -> None:
    async with Session() as s:
        await s.execute(update(A).where(A.id == oid).values(**values))
        await s.commit()


async def _refresh_account(acc_id: int, state: dict, proxy: str | None, title: str) -> None:
    """Заказы и баланс аккаунта — в БД сразу (как после оплаты), чтобы сайт показал новый заказ и деньги."""
    try:
        keeper._last.setdefault(acc_id, {}).update(dict.fromkeys(("orders", "balance"), datetime.now(timezone.utc).timestamp()))
        st, _ = await account_api.fetch(state, proxy, ("orders", "balance"))
        if st:
            await keeper._save_state(acc_id, st)
    except Exception as e:
        log.warning("autobuy: заказы/баланс %s не перечитаны: %s: %s", title, type(e).__name__, str(e)[:200])


def precheck(o, p: dict | None, grade: dict, bal: dict | None) -> tuple[str | None, int | None, int | None]:
    """Перед отправкой: → (причина не слать | None, total, final) за штуку. Цена выше подтверждённой — не шлём, ниже —
    шлём. Кошельком сразу: денег меньше суммы (без скидки Кошелька, если WB её снимет — discount_lost) — не шлём."""
    if not p:
        return "товар не найден" + NOT_SENT, None, None
    try:
        total, final = checkout.prices(p, account_api.parse_grade(grade) or 0)
    except ValueError:
        return "нет в наличии" + NOT_SENT, None, None
    if total > o.price_ok:
        return f"цена выросла: было {rub(o.price_ok)}, стало {rub(total)}", total, final
    if o.pay_now:
        if bal.get("wallet_blocked"):
            return "Кошелёк заблокирован — оплата им недоступна", total, final
        need = total * o.qty if discount_lost(final * o.qty, final, bal) else final * o.qty
        funds = _kop(bal, "funds")
        if funds is None or funds < need:
            return f"на Кошельке {rub(funds)}, нужно {rub(need)}", total, final
    return None, total, final


async def _fresh(o, pvz, state: dict, proxy: str | None):
    """Свежие detail (dest ПВЗ), grade и (Кошельком сразу) баланс. → (p, grade, bal) или текст ошибки."""
    try:
        p = await fetch_product(o.nm, pvz.dest)
        grade = await checkout.fetch_grade(state, proxy)
        bal = await account_api.fetch_balance(state, proxy) if o.pay_now else None
        return p, grade, bal
    except NoCard:
        return NO_CARD + NOT_SENT
    except (Unauthorized, NoBearer):
        return LOGIN
    except ProxyDown:
        return PROXY
    except Exception as e:
        log.warning("autobuy: заказ %s: проверка перед отправкой: %s: %s", o.id, type(e).__name__, str(e)[:200])
        return "WB не ответил при проверке цены" + NOT_SENT


async def _send(o, p: dict, grade: dict, pvz, acc_snap: dict, state: dict, proxy: str | None) -> dict:
    sign = checkout.payment_signer(state)
    point = {"id": pvz.id, "address": pvz.address, "dest": pvz.dest, "lat": pvz.lat, "lon": pvz.lon,
             "type_point": pvz.type_point, "sign": pvz.sign, "sign_v2": pvz.sign_v2}

    def make(sticker):
        return checkout.build_order(product=p, grade=grade, pvz=point, phone=acc_snap["phone"], name=acc_snap["name"],
                                    qty=o.qty, pay_now=o.pay_now, uid=o.uid, sticker=sticker, sign=sign)[0]
    try:
        return await checkout.place_order(state, proxy, o.uid, make)
    except (Unauthorized, NoBearer):
        return {"status": "error", "error": LOGIN, "rids": []}
    except ProxyDown:
        return {"status": "error", "error": PROXY, "rids": []}
    except ValueError as e:  # тело не собрать — ничего не отправлено
        return {"status": "error", "error": str(e) + NOT_SENT, "rids": []}
    except Exception as e:  # неожиданное — мог уйти
        log.warning("autobuy: заказ %s: %s: %s", o.id, type(e).__name__, str(e)[:200])
        return {"status": "unknown", "error": checkout.NO_ANSWER, "rids": []}


async def place(oid: int) -> None:
    """Один заказ: свежие цена/grade/баланс → проверки → sending (коммит ДО запроса) → place_order → итог."""
    async with Session() as s:
        o = await s.get(A, oid)
        acc = o and o.account_id and await s.get(models.WbAccount, o.account_id)
        if o is None or o.status != "pending":
            return
        if not acc:
            return await _finish(oid, status="error", error="аккаунт удалён", done_at=datetime.now(timezone.utc))
        pvz = await s.get(PP, o.pvz_id)
        snap = {"phone": acc.phone, "name": acc.name, "title": acc.title, "id": acc.id}
        state, proxy_id = decrypt_json(acc.state_enc), acc.proxy_id
    done = lambda status, error, **kw: _finish(oid, status=status, error=error and error[:512],
                                               done_at=datetime.now(timezone.utc), **kw)
    if not pvz or pvz.closed_at:
        return await done("error", "ПВЗ закрыт — выберите другой" + NOT_SENT)
    proxy = await keeper.proxy_url(proxy_id)
    fresh = await _fresh(o, pvz, state, proxy)
    if isinstance(fresh, str):
        return await done("error", fresh)
    p, grade, bal = fresh
    why, total, final = precheck(o, p, grade, bal)
    if why:
        return await done("error", why, total=total, final=final)
    await _finish(oid, status="sending", total=total, final=final)
    res = await _send(o, p, grade, pvz, snap, state, proxy)
    await done(res["status"], res["error"], rids=res["rids"])
    log.info("autobuy: заказ %s (%s, nm %s × %d, %s, %s за шт): %s %s", oid, snap["title"], o.nm, o.qty,
             "Кошельком сразу" if o.pay_now else "при получении", rub(total), res["status"], res["error"] or "")
    if res["status"] != "error" or res["rids"]:
        await _refresh_account(snap["id"], state, proxy, snap["title"])


async def cancel(oid: int) -> None:
    """Отмена позиций заказа (rids). WB начал отмену по всем → cancelled; иначе cancel_state error с причиной."""
    async with Session() as s:
        o = await s.get(A, oid)
        acc = o and o.account_id and await s.get(models.WbAccount, o.account_id)
        if o is None or o.cancel_state != "pending":
            return
        rids, title = list(o.rids or []), acc and acc.title
        state, proxy_id = (decrypt_json(acc.state_enc), acc.proxy_id) if acc else (None, None)
    fail = lambda text: _finish(oid, cancel_state="error", cancel_error=text[:256])
    if not acc:
        return await fail("аккаунт удалён")
    if not rids:
        return await fail("нечего отменять — у заказа нет позиций")
    proxy = await keeper.proxy_url(proxy_id)
    try:
        started = set(await checkout.cancel_order(state, proxy, rids)) & set(rids)
    except (Unauthorized, NoBearer):
        return await fail(LOGIN)
    except ProxyDown:
        return await fail(PROXY)
    except Exception as e:
        log.warning("autobuy: отмена %s (%s): %s: %s", oid, title, type(e).__name__, str(e)[:200])
        return await fail("WB не ответил — попробуйте ещё раз")
    if started == set(rids):
        await _finish(oid, status="cancelled", cancel_state=None, cancel_error=None)
    else:
        await fail(f"WB начал отмену {len(started)} из {len(rids)} позиций — проверьте заказы аккаунта")
    log.info("autobuy: отмена %s (%s): %d из %d", oid, title, len(started), len(rids))
    await _refresh_account(acc.id, state, proxy, title)


async def on_restart() -> int:
    """Рестарт посреди отправки — мог уйти: sending → unknown (не повторяем)."""
    async with Session() as s:
        n = (await s.execute(update(A).where(A.status == "sending").values(
            status="unknown", error=checkout.NO_ANSWER, done_at=datetime.now(timezone.utc)))).rowcount
        await s.commit()
    return n


async def sweep_orphans(s) -> int:
    """Аккаунт удалили, пока заказ/отмена ждали очереди (очередь прокси их не увидит)."""
    now = datetime.now(timezone.utc)
    n = (await s.execute(update(A).where(A.status == "pending", A.account_id.is_(None)).values(
        status="error", error="аккаунт удалён", done_at=now))).rowcount
    n += (await s.execute(update(A).where(A.cancel_state == "pending", A.account_id.is_(None)).values(
        cancel_state="error", cancel_error="аккаунт удалён"))).rowcount
    return n


async def fail_place(oid: int, e: Exception) -> None:
    """Неожиданное исключение в place: sending → unknown (мог уйти), pending → error (не отправляли)."""
    async with Session() as s:
        now = datetime.now(timezone.utc)
        await s.execute(update(A).where(A.id == oid, A.status == "sending").values(
            status="unknown", error=checkout.NO_ANSWER, done_at=now))
        await s.execute(update(A).where(A.id == oid, A.status == "pending").values(
            status="error", error=f"ошибка: {type(e).__name__}" + NOT_SENT, done_at=now))
        await s.commit()


async def fail_cancel(oid: int, e: Exception) -> None:
    await _finish(oid, cancel_state="error", cancel_error=f"ошибка: {type(e).__name__}")
