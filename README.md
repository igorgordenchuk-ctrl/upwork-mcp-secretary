# upwork-mcp-secretary

**Upwork прямо в Claude + личный секретарь вакансий в Telegram.**
Через официальный MCP-сервер Upwork — без браузера, расширений и капч.

> *English:* a tiny CLI for the official Upwork MCP server (`https://mcp.upwork.com/mcp`), an hourly
> job-watch script that filters postings by your own rules and sends the best ones to your Telegram,
> and a Claude Code skill. Personal use only. Not affiliated with Upwork.

---

## Что это

С августа 2026 у Upwork есть официальный MCP-сервер: вы входите своим аккаунтом, и ИИ-агент видит
то же, что вы, — вакансии, клиентов, сообщения, отклики, Connects. В пакете три вещи:

| | что делает |
|---|---|
| `upmcp.py` | командная строка к серверу Upwork: вход, сводка, поиск, вакансия целиком, любой вызов |
| `secretary.py` | **личный секретарь**: раз в час ищет свежие вакансии, отсеивает по вашим правилам и присылает лучшие вам в Telegram |
| `skill/upwork` | скил для Claude Code: «что нового на апворке?», «разбери эту вакансию», «напиши отклик» |

Так выглядит карточка секретаря (пример, данные условные):

```
Power BI Developer for Finance Dashboards
💵 почасовая $45–$70/ч · 3 to 6 months
🕐 2 ч назад · ✋ откликов 5 to 10
🌍 United States · потрачено $48,200 · контрактов: 31 · 4.9★ (24) · нанимает в 71%
🎫 отклик: 16 Connects (на счету 40)
Открыть вакансию
```

## Что нужно

- аккаунт Upwork (фрилансер или агентство);
- Python 3.10+ и `pip install requests`;
- для секретаря — свой Telegram-бот (2 минуты, шаг 4);
- для скила — [Claude Code](https://claude.com/claude-code).

## Установка

**1. Скачать.** ZIP со страницы релиза или `git clone https://github.com/igorgordenchuk-ctrl/upwork-mcp-secretary`.
Положите папку в домашнюю: `~/upwork-mcp-secretary` (Windows: `C:\Users\<вы>\upwork-mcp-secretary`) —
там её ищет скил.

**2. Зависимость.**
```
pip install requests
```

**3. Вход в Upwork** — один раз, в браузере на этом же компьютере:
```
python upmcp.py login
```
Войдите и нажмите «Allow». Проверка:
```
python upmcp.py dashboard
```
Доступ продлевается сам. Токен лежит в `~/.upwork-mcp/token.json`. Отозвать: `python upmcp.py logout`
или Upwork → Settings → Connected Apps. **Один вход — один компьютер**: при продлении Upwork гасит
старый ключ, поэтому на второй машине делайте свой `login`, а не копию файла.

**4. Telegram-бот для секретаря.**
- в Telegram откройте @BotFather → `/newbot` → получите токен;
- напишите своему боту любое сообщение;
- откройте в браузере `https://api.telegram.org/bot<ТОКЕН>/getUpdates` — в ответе `"chat":{"id": …}`,
  это ваш `chat_id`;
- скопируйте `config.example.json` → `config.json` и впишите токен и `chat_id`.

**5. Проверка секретаря.**
```
python secretary.py test          # придёт «Секретарь Upwork на связи»
python secretary.py once --dry    # покажет карточки в консоли, ничего не отправит
python secretary.py once          # настоящий прогон
```

**6. Расписание — раз в час.**

Windows (одна строка, путь к python — свой):
```
schtasks /create /tn UpworkSecretary /sc hourly /mo 1 /tr "python \"%USERPROFILE%\upwork-mcp-secretary\secretary.py\" once"
```
macOS / Linux (`crontab -e`):
```
7 * * * * cd ~/upwork-mcp-secretary && python3 secretary.py once >> secretary.log 2>&1
```

## Скил для Claude Code

Скопируйте папку `skill/upwork` в `~/.claude/skills/upwork` (Windows: `C:\Users\<вы>\.claude\skills\upwork`)
и впишите в `SKILL.md` свой раздел **«Мои правила»** — ставку, стоп-критерии, стек. Дальше просто
пишите в Claude Code: «что нового на апворке?», «разбери вакансию <ссылка>», «напиши отклик».

Можно и без скила — подключить сам сервер Upwork прямо в Claude Code:
```
claude mcp add --transport http upwork https://mcp.upwork.com/mcp
```
затем `/mcp` → вход. Скил добавляет к этому ваши правила отбора и экономит контекст: Claude зовёт
короткие команды CLI, а не держит в памяти схемы тридцати инструментов.

## Правила секретаря (`config.json`)

| ключ | что значит |
|---|---|
| `queries` | что искать: `title` — слова в заголовке, `skills` — навыки Upwork (точные названия), `smart_search` — лента под ваш профиль |
| `verified_payment_only` | только клиенты с подтверждённой оплатой (фильтрует сам Upwork) |
| `max_proposals` | больше откликов — не показывать |
| `max_age_hours` | старше — не показывать |
| `min_client_spent` | сколько клиент должен был потратить на Upwork за всё время, $ |
| `min_client_rating` | оценка клиента от фрилансеров (если отзывы есть) |
| `min_hourly_max` | верх почасовой вилки ниже этого — отказ |
| `min_fixed_budget` | фикс меньше этого — отказ |
| `max_interviewing` | клиент уже собеседует столько — отказ |
| `skip_if_hired` | клиент уже нанял или сделал оффер — отказ |
| `title_must_match` / `title_stop` | регулярные выражения по заголовку: обязательно / запрещено |
| `max_details_per_run` | сколько вакансий открывать целиком за прогон (запросы дозируются) |
| `quiet_hours` | `[22, 8]` — ночью сообщения приходят без звука |

Совет: ищите **узко** — `title` и `skills`. Параметр `query` у Upwork ищет «по смыслу» и на
«business central report» приносит рекламщиков Amazon.

## Команды CLI

```
python upmcp.py whoami                 ваши аккаунты
python upmcp.py dashboard              приглашения, офферы, сообщения, Connects
python upmcp.py jobs "power bi"        свежие вакансии по словам в заголовке
python upmcp.py feed --days 1          лента Most Recent под ваш профиль
python upmcp.py job <ссылка>           вакансия целиком: клиент, воронка найма, цена отклика
python upmcp.py tools                  все инструменты сервера
python upmcp.py help find_jobs         справка и параметры инструмента
python upmcp.py call find_jobs search '{"skills":["Microsoft Power BI"],"sort":"recency"}'
```

## Безопасность

- **Всё, что меняет данные в Upwork** (отклик, сообщение, сохранение вакансии), CLI выполняет только с
  флагом `--write`. Отклик — в два шага: черновик-превью, потом отдельное подтверждение.
- **Офферы и оплата — только ручное подтверждение** на upwork.com: сервер возвращает ссылку, а не
  выполняет действие. Агент не потратит ваши деньги и не подпишет контракт за вас.
- Текст вакансий приходит помеченным как «чужой» — скил велит не выполнять инструкции из него.

## Правила Upwork (API & MCP Terms) — важно

- **Только для себя.** Не пересылайте вакансии и данные клиентов в группы и каналы, не продавайте.
- Не храните выдачу дольше 30 дней (секретарь чистит базу сам).
- Не смешивайте с данными, собранными браузером (скрейпинг, расширения).
- Ищите узко, не обходите «всю ленту подряд».
- Инструмент бесплатный; продавать его или сервис на нём без письменного согласия Upwork нельзя.

## Чего MCP не даёт

- **Сколько клиент реально платит в час** (`avg hourly rate paid`) — её смотрите на странице вакансии.
  Считать «потрачено ÷ часы» не стоит: в трату входят фиксы, цифра врёт.
- У поиска по словам нет фильтра по дате — по дате фильтрует только лента (`smart_search`).
- На плане Basic число откликов приходит корзиной («5 to 10», «50+»), точное — на Freelancer Plus.

---

Неофициальный инструмент, с Upwork не связан. Upwork — товарный знак Upwork Inc.
Сделано в сообществе [Power BI Pro](https://t.me/PBI_Pro). Лицензия — MIT.
