# -*- coding: utf-8 -*-
"""upmcp — командная строка к официальному Upwork MCP (https://mcp.upwork.com/mcp).

Upwork с августа 2026 даёт официальный MCP-сервер: ваш аккаунт доступен ИИ-агенту.
Эта утилита ходит в него напрямую — без браузера и без Claude, поэтому её можно
запускать по расписанию (секретарь вакансий — secretary.py) и звать из скила Claude.

Вход: OAuth 2.1 + PKCE, клиент регистрируется сам. Вы логинитесь в браузере ОДИН раз,
дальше доступ продлевается сам. Токен — ~/.upwork-mcp/token.json (или UPWORK_MCP_TOKEN).
Отозвать доступ: `python upmcp.py logout` или Upwork → Settings → Connected Apps.

Команды:
    python upmcp.py login                      — вход (откроется браузер на ЭТОМ компьютере)
    python upmcp.py logout                     — отозвать доступ и удалить токен
    python upmcp.py whoami                     — ваши аккаунты, проверка входа
    python upmcp.py dashboard                  — приглашения, офферы, сообщения, Connects
    python upmcp.py jobs "power bi"            — свежие вакансии с этими словами в заголовке
    python upmcp.py feed [--days N]            — лента Most Recent под ваш профиль
    python upmcp.py job <ссылка|id>            — вакансия целиком: клиент, воронка, Connects
    python upmcp.py tools                      — все инструменты сервера (чтение / ЗАПИСЬ)
    python upmcp.py help <инструмент>          — справка и схема инструмента
    python upmcp.py call <инструмент> <действие> ['<params json>'] [--write]
                                               — любой вызов; org_uid подставляется сам;
                                                 всё, что меняет данные, — только с --write

Нужно: Python 3.10+, `pip install requests`. Неофициальный инструмент, с Upwork не связан.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

TOKEN_FILE = Path(os.environ.get("UPWORK_MCP_TOKEN") or Path.home() / ".upwork-mcp" / "token.json")
SERVER = "https://mcp.upwork.com/mcp"
PROTOCOL = "2025-06-18"
REDIRECT_PORT = int(os.environ.get("UPWORK_MCP_PORT") or 53682)
REDIRECT = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
CLIENT_NAME = "upwork-mcp-secretary (CLI)"
PREFIX = "upwork__"                               # так шлюз Upwork называет инструменты
NO_ORG = {"list_accounts", "get_tool_help"}       # единственные, кому org_uid не нужен

S = requests.Session()
S.headers["User-Agent"] = "upwork-mcp-secretary/1.0"


class UpworkError(RuntimeError):
    """Отказ сервера или инструмента — с причиной, которую можно показать человеку."""


# ------------------------------------------------------------------ токен

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def load() -> dict:
    if not TOKEN_FILE.exists():
        raise UpworkError(f"нет входа в Upwork ({TOKEN_FILE}) — нужен `python upmcp.py login`")
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))


def save(tok: dict) -> None:
    """Атомарно: оборванная запись не должна уничтожить refresh-токен."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, TOKEN_FILE)
    try:
        os.chmod(TOKEN_FILE, 0o600)                 # на Windows ничего не делает — и не нужно
    except OSError:
        pass


class _Lock:
    """Межпроцессный замок на продление токена.

    Refresh-токен одноразовый: при продлении Upwork выдаёт новый и гасит старый. Два
    процесса, продлившие его одновременно, оставят в файле погашенный — и вход умрёт.
    По той же причине один токен нельзя делить между двумя компьютерами: у каждого свой login.
    """

    def __init__(self) -> None:
        self.path = TOKEN_FILE.with_suffix(".lock")
        self.fd = None

    def __enter__(self):
        deadline = time.time() + 90
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 120:   # хозяин замка умер
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise UpworkError("замок токена занят больше 90 с")
                time.sleep(0.5)

    def __exit__(self, *exc):
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


def auth_meta() -> dict:
    """Адреса входа берём у сервера, а не зашиваем: Upwork может их сменить."""
    base = SERVER.rsplit("/", 1)[0]
    r = S.get(base + "/.well-known/oauth-protected-resource/mcp", timeout=30)
    r.raise_for_status()
    issuer = r.json()["authorization_servers"][0].rstrip("/")
    r = S.get(issuer + "/.well-known/oauth-authorization-server", timeout=30)
    r.raise_for_status()
    return r.json()


def _token_request(tok: dict, data: dict) -> dict:
    data = {**data, "client_id": tok["client_id"], "resource": SERVER}
    auth = None
    if tok.get("client_secret"):
        if tok.get("auth_method") == "client_secret_basic":
            auth = (tok["client_id"], tok["client_secret"])
        else:
            data["client_secret"] = tok["client_secret"]
    r = S.post(tok["token_endpoint"], data=data, auth=auth, timeout=60)
    if r.status_code >= 400:
        raise UpworkError(f"токен: {r.status_code} {r.text[:300]}")
    got = r.json()
    tok["access_token"] = got["access_token"]
    if got.get("refresh_token"):
        tok["refresh_token"] = got["refresh_token"]
    tok["expires_at"] = int(time.time()) + int(got.get("expires_in") or 3600)
    tok["refreshed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return tok


def refresh(force: bool = False) -> dict:
    with _Lock():
        tok = load()                           # перечитываем: соседний процесс мог уже продлить
        if not force and tok.get("expires_at", 0) - 120 > time.time():
            return tok
        if not tok.get("refresh_token"):
            raise UpworkError("refresh-токена нет — нужен повторный `python upmcp.py login`")
        try:
            tok = _token_request(tok, {"grant_type": "refresh_token",
                                       "refresh_token": tok["refresh_token"]})
        except UpworkError as e:
            if "invalid_grant" in str(e):
                raise UpworkError("вход в Upwork протух или отозван — нужен повторный "
                                  "`python upmcp.py login`") from None
            raise
        save(tok)
        return tok


def access_token() -> str:
    tok = load()
    if tok.get("expires_at", 0) - 120 <= time.time():
        tok = refresh()
    return tok["access_token"]


def login() -> None:
    meta = auth_meta()
    reg = S.post(meta["registration_endpoint"], timeout=30, json={
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    if reg.status_code >= 400:
        raise UpworkError(f"регистрация клиента: {reg.status_code} {reg.text[:300]}")
    client = reg.json()

    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": SERVER,
    })

    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            body = ("<h2>Готово. Окно можно закрыть.</h2>" if ok else
                    f"<h2>Вход не удался: {got.get('error_description') or got.get('error') or 'нет кода'}</h2>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif'>{body}</body></html>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    srv.timeout = 1
    print("Открываю браузер для входа в Upwork. Если не открылся — откройте адрес вручную\n"
          "(на ЭТОМ компьютере: ответ Upwork придёт на 127.0.0.1):\n" + url, flush=True)
    webbrowser.open(url)
    deadline = time.time() + 600
    while not got and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if not got:
        raise UpworkError("за 10 минут вход не завершён")
    if got.get("state") != state:
        raise UpworkError("ответ входа не от нашего запроса (state не совпал)")
    if "code" not in got:
        raise UpworkError(f"вход отклонён: {got.get('error_description') or got.get('error')}")

    tok = {"client_id": client["client_id"], "client_secret": client.get("client_secret"),
           "auth_method": client.get("token_endpoint_auth_method", "none"),
           "token_endpoint": meta["token_endpoint"],
           "revocation_endpoint": meta.get("revocation_endpoint"),
           "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    tok = _token_request(tok, {"grant_type": "authorization_code", "code": got["code"],
                               "redirect_uri": REDIRECT, "code_verifier": verifier})
    save(tok)
    print(f"вход сохранён: {TOKEN_FILE}")


def logout() -> None:
    """Отозвать токен у Upwork и удалить файл. Не вышло отозвать — всё равно удаляем
    и подсказываем, где отключить вручную."""
    try:
        tok = load()
    except UpworkError:
        print("входа нет — нечего отзывать")
        return
    endpoint = tok.get("revocation_endpoint") or "https://www.upwork.com/api/v3/oauth2/token/revoke"
    ok = False
    for kind in ("refresh_token", "access_token"):
        if tok.get(kind):
            r = S.post(endpoint, data={"token": tok[kind], "token_type_hint": kind,
                                       "client_id": tok["client_id"]}, timeout=30)
            ok = ok or r.status_code < 400
    TOKEN_FILE.unlink(missing_ok=True)
    print("доступ отозван, токен удалён" if ok else
          "токен удалён; отозвать у Upwork не удалось — отключите вручную: "
          "Upwork → Settings → Connected Apps")


# -------------------------------------------------------------- MCP-сессия

class Mcp:
    """Минимальный клиент Streamable HTTP: initialize → tools/list | tools/call."""

    def __init__(self) -> None:
        self.sid: str | None = None
        self.proto = PROTOCOL
        self.n = 0
        self.ready = False
        self.server_info: dict = {}
        self._tools: list[dict] | None = None

    def _post(self, payload: dict, retried: bool = False):
        h = {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        if self.ready:
            h["MCP-Protocol-Version"] = self.proto
        r = S.post(SERVER, json=payload, headers=h, timeout=180, stream=True)
        if r.status_code == 401 and not retried:
            refresh(force=True)
            return self._post(payload, retried=True)
        if r.status_code == 429:
            raise UpworkError(f"лимит запросов Upwork (429), повтор через "
                              f"{r.headers.get('Retry-After', '?')} с")
        if r.status_code >= 400:
            raise UpworkError(f"MCP {r.status_code}: {r.text[:300]}")
        self.sid = r.headers.get("Mcp-Session-Id") or self.sid
        if "id" not in payload:                # уведомление: ответа нет
            return None
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            return self._sse(r, payload["id"])
        return r.json()

    @staticmethod
    def _sse(r, want_id):
        data: list[str] = []
        for line in r.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
                continue
            if line == "" and data:
                msg = json.loads("\n".join(data))
                data = []
                if msg.get("id") == want_id and ("result" in msg or "error" in msg):
                    return msg
        raise UpworkError("поток ответа закрылся без результата")

    def rpc(self, method: str, params: dict | None = None):
        if not self.ready and method != "initialize":
            self.start()
        self.n += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if "error" in msg:
            raise UpworkError(f"{method}: {msg['error']}")
        return msg["result"]

    def start(self) -> None:
        init = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "upwork-mcp-secretary", "version": "1.0"}})
        self.proto = init.get("protocolVersion") or PROTOCOL
        self.ready = True
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.server_info = init.get("serverInfo", {})

    def tools(self) -> list[dict]:
        if self._tools is None:
            out, cursor = [], None
            while True:
                res = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
                out += res.get("tools", [])
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            self._tools = out
        return self._tools

    def call(self, name: str, args: dict | None = None):
        """Вызов инструмента → разобранный ответ. Ошибка инструмента → UpworkError.
        Шлюз зовёт инструменты `upwork__find_jobs`, справка — `find_jobs`: принимаем оба."""
        if not name.startswith(PREFIX):
            name = PREFIX + name
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        data = res.get("structuredContent")
        if data is None:
            text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = text
        if res.get("isError"):
            raise UpworkError(f"{name}: {json.dumps(data, ensure_ascii=False)[:500]}")
        return data


# ------------------------------------------------------------ Upwork-обёртки

def org_uid(m: Mcp) -> str:
    """org_uid фрилансерского аккаунта. Спрашиваем один раз и кладём рядом с токеном."""
    tok = load()
    if tok.get("org_uid"):
        return tok["org_uid"]
    accs = m.call("list_accounts").get("accounts", [])
    talent = [a for a in accs if a.get("role") in ("TALENT", "FL_AGENCY")]
    if not talent:
        raise UpworkError("в аккаунте нет роли фрилансера или агентства")
    with _Lock():
        tok = load()
        tok["org_uid"] = talent[0]["org_uid"]
        save(tok)
    return tok["org_uid"]


def find_jobs(m: Mcp, action: str, **params) -> dict:
    return m.call("find_jobs", {"action": action, "org_uid": org_uid(m), "params": params})


# --------------------------------------------------------------------- CLI

def _print(x) -> None:
    print(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, indent=2))


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else ""
    if cmd == "login":
        login()
        return
    if cmd == "logout":
        logout()
        return
    m = Mcp()
    if cmd == "whoami":
        m.start()
        print(f"сервер: {m.server_info.get('name')} · протокол {m.proto}")
        _print(m.call("list_accounts"))
    elif cmd == "dashboard":
        _print(m.call("get_freelancer_dashboard", {"action": "check", "org_uid": org_uid(m)}))
    elif cmd == "jobs" and len(argv) > 1:
        _print(find_jobs(m, "search", title=argv[1], sort="recency", limit=10))
    elif cmd == "feed":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 1
        _print(find_jobs(m, "smart_search", mode="most_recent", days_posted=days, limit=10))
    elif cmd == "job" and len(argv) > 1:
        _print(find_jobs(m, "get", id=argv[1]))
    elif cmd == "tools":
        for t in m.tools():
            ro = (t.get("annotations") or {}).get("readOnlyHint")
            desc = (t.get("description") or "").strip().splitlines()
            print(f"{t['name'].removeprefix(PREFIX):<30} {'чтение' if ro else 'ЗАПИСЬ':<7} "
                  f"{desc[0][:100] if desc else ''}")
    elif cmd == "help" and len(argv) > 1:
        _print(m.call("get_tool_help", {"tool_name": argv[1].removeprefix(PREFIX)}))
    elif cmd == "call" and len(argv) > 1:
        write = "--write" in argv
        rest = [a for a in argv[2:] if a != "--write"]
        tool = argv[1].removeprefix(PREFIX)
        if rest and not rest[0].lstrip().startswith("{"):          # <действие> ['<params>']
            args = {"action": rest[0], "params": json.loads(rest[1]) if len(rest) > 1 else {}}
        else:                                                       # сырой '<json>'
            args = json.loads(rest[0]) if rest else {}
        if tool not in NO_ORG and "org_uid" not in args:
            args["org_uid"] = org_uid(m)
        # Предохранитель: всё, что меняет состояние в Upwork, — только с явным --write.
        # Чтобы опечатка или чужая инструкция в тексте вакансии не стала отправленным откликом.
        info = next((t for t in m.tools() if t["name"] == PREFIX + tool), {})
        if (info.get("annotations") or {}).get("readOnlyHint") is not True and not write:
            raise UpworkError(f"{tool} меняет данные в Upwork — запуск только с --write "
                              f"и только после вашего явного «да»")
        _print(m.call(tool, args))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main(sys.argv[1:])
    except UpworkError as e:
        print(f"! {e}", file=sys.stderr)
        sys.exit(1)
