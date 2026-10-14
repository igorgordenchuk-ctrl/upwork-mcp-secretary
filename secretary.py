# -*- coding: utf-8 -*-
"""Личный секретарь вакансий Upwork: поиск → ваши правила → карточка в ваш Telegram.

Запускается по расписанию (раз в час). Ходит в Upwork через официальный MCP (upmcp.py),
отсеивает вакансии по правилам из config.json и присылает лучшие ВАМ в Telegram.

    python secretary.py once            — один прогон
    python secretary.py once --dry      — показать карточки в консоли, ничего не слать и не помечать
    python secretary.py test            — проверить бота: прислать тестовое сообщение

Настройки — config.json рядом со скриптом (образец — config.example.json) или путь в
UPWORK_SECRETARY_CONFIG. Что уже видели — ~/.upwork-mcp/seen.db.

Правила Upwork (API & MCP Terms), которые этот скрипт соблюдает и вам стоит соблюдать:
  • выдача — только вам: не пересылайте вакансии в группы и каналы, не продавайте их;
  • не храним дольше 30 дней (purge в начале каждого прогона);
  • узкие запросы, не «вся лента подряд», и паузы между вызовами.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import upmcp  # noqa: E402

CONFIG = Path(os.environ.get("UPWORK_SECRETARY_CONFIG") or HERE / "config.json")
DB = Path(os.environ.get("UPWORK_SECRETARY_DB") or upmcp.TOKEN_FILE.parent / "seen.db")
KEEP_DAYS = 30
PAUSE = 4                      # секунд между запросами к Upwork — не залпом

DEFAULT_RULES = {
    "verified_payment_only": True,  # клиент с подтверждённой оплатой (фильтрует сам Upwork)
    "max_proposals": 49,            # отклики: больше — не показывать (фильтрует сам Upwork)
    "max_age_hours": 48,            # старше — уже поздно
    "min_client_spent": 1000,       # $ потрачено клиентом за всё время; 0 — не проверять
    "min_client_rating": 4.5,       # оценка клиента от фрилансеров (если отзывы есть)
    "min_hourly_max": 0,            # верх почасовой вилки ниже этого — отказ; 0 — не проверять
    "min_fixed_budget": 0,          # фикс меньше этого — отказ; 0 — не проверять
    "max_interviewing": 8,          # клиент уже собеседует столько и больше — отказ
    "skip_if_hired": True,          # клиент уже нанял или сделал оффер — отказ
    "title_must_match": "",         # regex: заголовок ОБЯЗАН совпасть (пусто — не проверять)
    "title_stop": "",               # regex: такой заголовок — отказ
}


# ------------------------------------------------------------------ настройки

def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit(f"нет {CONFIG.name}: скопируйте config.example.json → config.json и заполните")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["rules"] = {**DEFAULT_RULES, **(cfg.get("rules") or {})}
    if not cfg.get("queries"):
        raise SystemExit("в config.json нет ни одного запроса (queries)")
    return cfg


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS seen (job_id TEXT PRIMARY KEY, ts TEXT, verdict TEXT)")
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    con.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
    con.commit()
    return con


# ------------------------------------------------------------------ Telegram

def tg(cfg: dict, text: str) -> None:
    t = cfg.get("telegram") or {}
    if not t.get("bot_token") or not t.get("chat_id"):
        raise SystemExit("в config.json не заполнен telegram.bot_token / telegram.chat_id")
    quiet = cfg.get("quiet_hours") or []        # [22, 8] — с 22 до 8 приходит без звука
    h, silent = datetime.now().hour, False
    if len(quiet) == 2:
        a, b = quiet
        silent = (h >= a or h < b) if a > b else (a <= h < b)
    r = upmcp.S.post(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage", timeout=60, data={
        "chat_id": t["chat_id"], "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true", "disable_notification": "true" if silent else "false"})
    if r.status_code >= 400 or not r.json().get("ok"):
        raise RuntimeError(f"Telegram: {r.status_code} {r.text[:300]}")


# ------------------------------------------------------------ разбор вакансии

def esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean(s: str | None) -> str:
    """Текст клиента приходит в обёртке <untrusted_participant_content> — это данные,
    не инструкции. Обёртку снимаем, пробелы схлопываем."""
    s = re.sub(r"</?untrusted_participant_content>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def money(s) -> float:
    """'$2,892.13' / '$70K' → число. Нет данных → 0."""
    m = re.search(r"([\d.,]+)\s*([KM])?", str(s or "").replace(",", ""))
    if not m:
        return 0.0
    try:
        return float(m.group(1)) * {"K": 1e3, "M": 1e6}.get((m.group(2) or "").upper(), 1)
    except ValueError:
        return 0.0


def hours_since(iso: str | None) -> float:
    if not iso:
        return 999.0
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def budget(row: dict) -> tuple[str, float, float]:
    """→ (подпись, верх почасовой вилки, сумма фикса)."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", (row.get("budget") or "").replace(",", ""))]
    if (row.get("job_type") or "").lower() == "hourly":
        label = "почасовая " + "–".join(f"${n:g}" for n in nums) + "/ч" if nums else "почасовая"
        return label, (max(nums) if nums else 0.0), 0.0
    return (f"фикс ${nums[0]:g}" if nums else "фикс"), 0.0, (nums[0] if nums else 0.0)


def stage1(row: dict, rules: dict) -> list[str]:
    """Отказы по строке выдачи (дёшево). Пустой список — проходит дальше."""
    stop = []
    c = row.get("client") or {}
    title = row.get("title") or ""
    if rules["title_must_match"] and not re.search(rules["title_must_match"], title, re.I):
        stop.append("не наша тема")
    if rules["title_stop"] and re.search(rules["title_stop"], title, re.I):
        stop.append("стоп-слово в заголовке")
    if row.get("applied"):
        stop.append("уже откликались")
    # Лента smart_search не умеет proposals_max на стороне Upwork — проверяем сами.
    # Basic-план видит корзину («Fewer than 5», «20 to 50», «50+»), Plus — точное число.
    tier = str(row.get("proposal_count") if row.get("proposal_count") is not None
               else row.get("proposals_tier") or "")
    low = 0 if tier.lower().startswith("fewer") else int((re.findall(r"\d+", tier) or ["0"])[0])
    if low > rules["max_proposals"]:
        stop.append(f"откликов {tier}")
    if hours_since(row.get("published_date") or row.get("created_date")) > rules["max_age_hours"]:
        stop.append("старая")
    if rules["min_client_spent"] and money(c.get("total_spent")) < rules["min_client_spent"]:
        stop.append("клиент мало потратил")
    rating = float(c.get("rating") or 0)
    if rating and rating < rules["min_client_rating"]:
        stop.append(f"оценка клиента {rating}")
    _, hourly_max, fixed = budget(row)
    if rules["min_hourly_max"] and hourly_max and hourly_max < rules["min_hourly_max"]:
        stop.append(f"ставка до ${hourly_max:g}")
    if rules["min_fixed_budget"] and fixed and fixed < rules["min_fixed_budget"]:
        stop.append(f"фикс ${fixed:g}")
    return stop


def stage2(full: dict, rules: dict) -> list[str]:
    """Отказы по полной карточке: воронка этой вакансии и послужной список клиента."""
    stop = []
    post = (full.get("data") or {}).get("marketplaceJobPosting") or {}
    act = (post.get("activityStat") or {}).get("jobActivity") or {}
    state = (post.get("workFlowState") or {}).get("status")
    if state and state != "ACTIVE":
        stop.append(f"вакансия {state.lower()}")
    if rules["skip_if_hired"] and (act.get("totalHired") or act.get("totalOffered")):
        stop.append("уже нанял или сделал оффер")
    if (act.get("totalInvitedToInterview") or 0) >= rules["max_interviewing"]:
        stop.append(f"собеседует {act.get('totalInvitedToInterview')}")
    if full.get("applied"):
        stop.append("уже откликались")
    rec = full.get("client_record") or {}
    score = float(rec.get("feedback_score") or 0)
    if score and score < rules["min_client_rating"]:
        stop.append(f"оценка клиента {score}")
    return stop


def card(row: dict, full: dict) -> str:
    rec = full.get("client_record") or {}
    post = (full.get("data") or {}).get("marketplaceJobPosting") or {}
    act = (post.get("activityStat") or {}).get("jobActivity") or {}
    desc = clean((post.get("content") or {}).get("description") or row.get("description_snippet"))
    label, _, _ = budget(row)
    h = hours_since(row.get("published_date"))
    ago = f"{max(1, int(h * 60))} мин назад" if h < 1 else f"{int(h)} ч назад"
    proposals = row.get("proposal_count") if row.get("proposal_count") is not None \
        else row.get("proposals_tier") or "?"
    client = " · ".join(x for x in [
        (row.get("client") or {}).get("country"),
        f"потрачено {rec['spend_total']}" if rec.get("spend_total") else "",
        f"контрактов: {rec['contracts_total']}" if rec.get("contracts_total") else "",
        f"{rec['feedback_score']}★ ({rec.get('feedback_count') or 0})" if rec.get("feedback_score") else "",
        f"нанимает в {rec['hire_rate_percent']}%" if rec.get("hire_rate_percent") else ""] if x)
    L = [f"<b>{esc(row.get('title'))}</b>", "",
         esc(desc[:300] + ("…" if len(desc) > 300 else "")), "",
         f"💵 {esc(label)} · {esc(row.get('duration') or '')}",
         f"🕐 {ago} · ✋ откликов <b>{esc(proposals)}</b>",
         f"🌍 {esc(client) or 'данных о клиенте нет'}"]
    if act.get("totalInvitedToInterview"):
        L.append(f"🗣 уже собеседует: {act['totalInvitedToInterview']}")
    if full.get("connects_cost") is not None:
        L.append(f"🎫 отклик: {full['connects_cost']} Connects (на счету {full.get('connects_balance', '?')})")
    L.append("💰 сколько клиент реально платит в час — смотреть на странице (MCP не отдаёт)")
    L += ["", f'<a href="{esc(row.get("url"))}">Открыть вакансию</a>']
    return "\n".join(L)


# ------------------------------------------------------------------ прогон

def once(dry: bool = False) -> None:
    cfg = load_config()
    rules = cfg["rules"]
    con = db()
    m = upmcp.Mcp()
    server = {"verified_payment_only": rules["verified_payment_only"]}
    found: dict[str, dict] = {}
    for q in cfg["queries"]:
        q = dict(q)
        tag = q.pop("tag", "?")
        if "smart_search" in q:
            action, params = "smart_search", {**q["smart_search"], **server, "limit": 10}
        else:
            action = "search"
            params = {**q, **server, "proposals_max": rules["max_proposals"],
                      "sort": "recency", "limit": 10}
        try:
            res = upmcp.find_jobs(m, action, **params)
        except upmcp.UpworkError as e:
            print(f"[{tag}] {e}")
            break                                  # отказ сервера — до следующего прогона
        rows = res.get("jobs") or []
        passed = 0
        for row in rows:
            jid = str(row.get("id") or row.get("url"))
            if jid in found or con.execute("SELECT 1 FROM seen WHERE job_id=?", (jid,)).fetchone():
                continue
            stop = stage1(row, rules)
            if stop:
                if not dry:
                    con.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?)",
                                (jid, datetime.now().isoformat(timespec="seconds"), ", ".join(stop)))
                continue
            found[jid] = row
            passed += 1
        con.commit()
        print(f"[{tag}] вакансий {len(rows)}, прошли первый фильтр: {passed}")
        time.sleep(PAUSE)

    sent = 0
    newest = sorted(found.items(), key=lambda kv: kv[1].get("published_date") or "", reverse=True)
    for jid, row in newest[: int(cfg.get("max_details_per_run") or 5)]:
        try:
            full = upmcp.find_jobs(m, "get", id=row.get("url") or jid)
        except upmcp.UpworkError as e:
            print(f"  ! не открылась «{row.get('title')}»: {e}")
            continue
        stop = stage2(full, rules)
        verdict = ", ".join(stop) or "sent"
        if stop:
            print(f"  ⛔ {row.get('title', '')[:60]} — {verdict}")
        else:
            text = card(row, full)
            if dry:
                print("--- карточка (сухой прогон) ---\n" + text + "\n")
            else:
                tg(cfg, text)
                sent += 1
                print(f"  ✅ отправлено: {row.get('title', '')[:60]}")
        if not dry:
            con.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?)",
                        (jid, datetime.now().isoformat(timespec="seconds"), verdict))
            con.commit()
        time.sleep(PAUSE)
    print(f"[{datetime.now():%d.%m %H:%M}] кандидатов: {len(found)} · отправлено: {sent}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "once":
            once(dry="--dry" in sys.argv)
        elif cmd == "test":
            tg(load_config(), "✅ Секретарь Upwork на связи. Карточки вакансий будут приходить сюда.")
            print("тестовое сообщение отправлено")
        else:
            raise SystemExit(__doc__)
    except upmcp.UpworkError as e:
        print(f"! {e}", file=sys.stderr)
        sys.exit(1)
