#!/usr/bin/env python3
"""WhatsApp руководителя через шлюз Evolution API на этом Mac. Сервер не нужен.

Шлюз и база сообщений работают в Docker здесь же, адрес только локальный http://127.0.0.1:18080.
Ключ доступа и пароль базы создаются при первом запуске в .private/whatsapp.env и никуда не уходят.
Пока Mac выключен, WhatsApp держит новые сообщения у себя и отдаёт их шлюзу при следующем запуске.
Чтение и отправка сами запускают остановленный шлюз и догружают пропущенное.

  python3 tools/whatsapp.py status                   установлен ли шлюз и привязан ли телефон
  python3 tools/whatsapp.py up                       поднять шлюз и догрузить пропущенное
  python3 tools/whatsapp.py qr                       привязать телефон по QR
  python3 tools/whatsapp.py stop                     остановить шлюз, переписка в базе сохраняется
  python3 tools/whatsapp.py chats [--hours 24]       чаты с сообщениями за период
  python3 tools/whatsapp.py read [ЧАТ] [--hours 24]  переписка одного чата или всех чатов за период
  python3 tools/whatsapp.py send ЧАТ --to "Имя" "текст"   только после «да» руководителя; «-» вместо текста: из stdin

ЧАТ: id из списка chats, часть имени или номер телефона, если переписки ещё не было.
--to: имя получателя, как его показывают chats и read. Скрипт сверяет его с чатом, а руководитель видит его
в окне подтверждения. Для номера телефона --to не нужен.
Телефоны, ИИН и почты в выводе заменяются метками, как в tools/obezlichit.py.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from obezlichit import Masker, load_map, save_map  # noqa: E402

os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"  # Homebrew мог не попасть в PATH
sys.stdout.reconfigure(line_buffering=True)  # ход запуска виден сразу, а не в конце

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = ROOT / ".private" / "whatsapp.env"
COMPOSE = ROOT / "tools" / "whatsapp" / "compose.yml"
URL = "http://127.0.0.1:18080"
NAME = "shtab"
NUMBER = r"\+?[\d\s()\-]{10,}"
# Номера, которые не ловит obezlichit.py: 7701…, «7 701 …» без плюса и иностранные с плюсом
PHONE = re.compile(r"(?<![\w+])(?:\+\d[\d\s()\-]{8,16}\d|7[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})(?!\d)")
KINDS = {"imageMessage": "фото", "videoMessage": "видео", "documentMessage": "документ",
         "audioMessage": "голосовое", "stickerMessage": "стикер", "locationMessage": "геолокация",
         "contactMessage": "контакт"}


# ---------- шлюз: запуск, остановка, состояние ----------

def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def docker_ready():
    return bool(shutil.which("docker")) and run("docker", "info").returncode == 0


def start_docker():
    if docker_ready():
        return
    if shutil.which("colima"):
        print("Запускаю Colima, до 5 минут.")
        run("colima", "start", "--memory", "3")
    elif pathlib.Path("/Applications/Docker.app").exists():
        run("open", "-a", "Docker")
    else:
        sys.exit("Нет Docker. Поставьте Colima по навыку whatsapp, раздел «Поставить», и повторите.")
    for _ in range(90):
        if docker_ready():
            return
        time.sleep(2)
    sys.exit("Docker не запустился за 3 минуты. Выполните `colima start` или откройте Docker и посмотрите ошибку.")


def compose(*args):
    base = ["docker", "compose"] if run("docker", "compose", "version").returncode == 0 else ["docker-compose"]
    return run(*base, "-f", str(COMPOSE), *args)


def alive():
    try:
        urllib.request.urlopen(URL, timeout=3).close()
        return True
    except (OSError, ValueError):
        return False


def api(method, path, body=None, check=True):
    key = next(s.split("=", 1)[1] for s in ENV.read_text().splitlines() if s.startswith("AUTHENTICATION_API_KEY="))
    req = urllib.request.Request(URL + path, method=method, headers={"apikey": key, "Content-Type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "null", strict=False)
    except urllib.error.HTTPError as e:
        if not check:
            return None
        sys.exit(f"Шлюз ответил ошибкой {e.code}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError:
        sys.exit("Шлюз не отвечает. Запустите: python3 tools/whatsapp.py up")
    except OSError:
        sys.exit("Шлюз не ответил вовремя. Если это была отправка, повторно не отправляй: "
                 "сначала проверь read <id> --hours 1.")


def state():
    """open — телефон привязан, connecting — подключается или ждёт QR, close — не привязан, None — ещё не загрузился."""
    return ((api("GET", f"/instance/connectionState/{NAME}") or {}).get("instance") or {}).get("state")


def linked():
    """True — привязан, False — не привязан или ждёт QR, None — ещё подключается."""
    for _ in range(5):  # привязанный шлюз после сна Mac переподключается за секунды
        st = state()
        if st == "open":
            return True
        if st != "connecting":
            return None if st is None else False
        time.sleep(2)
    # подключение затянулось: если шлюз показывает QR, телефон не привязан. В этом состоянии connect только отдаёт QR
    return False if (api("GET", f"/instance/connect/{NAME}") or {}).get("base64") else None


def since_wake():
    """Секунды с последнего пробуждения Mac."""
    m = re.search(r"sec = (\d+)", run("sysctl", "-n", "kern.waketime").stdout)
    return time.time() - int(m.group(1)) if m else 1e9


def make_env():
    ENV.parent.mkdir(mode=0o700, exist_ok=True)
    pw = secrets.token_hex(16)
    ENV.write_text(f"AUTHENTICATION_API_KEY={secrets.token_hex(24)}\nPOSTGRES_PASSWORD={pw}\n"
                   f"DATABASE_CONNECTION_URI=postgresql://evolution:{pw}@db:5432/evolution?schema=evolution_api\n")
    ENV.chmod(0o600)


def start():
    """Поднимает шлюз, если он не отвечает. True, если пришлось поднимать."""
    if alive():
        return False
    start_docker()
    print("Поднимаю шлюз WhatsApp. В первый раз скачивается около 500 МБ, это несколько минут.")
    r = compose("up", "-d")
    if r.returncode:
        sys.exit("Шлюз не поднялся:\n" + (r.stderr or r.stdout)[-1500:])
    for _ in range(120):
        if alive():
            return True
        time.sleep(2)
    sys.exit("Шлюз не ответил за 4 минуты. Покажи этот текст руководителю и запиши в обучение/вопросы_к_созвону.md.")


def ensure_instance():
    """Создаёт подключение, если его нет. Проверяем по базе: после запуска шлюз загружает его не сразу."""
    if any(i.get("name") == NAME for i in api("GET", "/instance/fetchInstances") or []):
        return False
    api("POST", "/instance/create", {"instanceName": NAME, "integration": "WHATSAPP-BAILEYS", "syncFullHistory": True})
    return True


def total():
    return api("POST", f"/chat/findMessages/{NAME}", {"where": {}, "offset": 1})["messages"]


def catch_up():
    """После запуска или сна Mac WhatsApp отдаёт накопленное. Ждём, пока поток утихнет."""
    left = 60 - since_wake()
    if left > 0:  # после сна шлюз замечает обрыв связи и переподключается не сразу
        print("Mac недавно проснулся, жду, пока шлюз переподключится к WhatsApp.")
        time.sleep(left)
    for _ in range(5):
        ok = linked()
        if ok:
            break
        if ok is False:
            return
        time.sleep(2)
    else:
        return
    print("Догружаю сообщения, пришедшие, пока шлюз был без связи.")
    last, calm = -1, 0
    for _ in range(24):
        time.sleep(5)
        n = total()["total"]
        calm = calm + 1 if n == last else 0
        if calm == 2:
            return
        last = n


def ready():
    """Поднимает шлюз, догружает пропущенное. Возвращает linked()."""
    if not ENV.exists():
        sys.exit("WhatsApp не подключён: шлюз не установлен. Подключите по навыку whatsapp, раздел «Поставить».")
    if start() or since_wake() < 180:
        catch_up()
    return linked()


def warn_unlinked():
    print("Телефон не привязан к шлюзу: показываю то, что уже сохранено. Привязать: python3 tools/whatsapp.py qr",
          file=sys.stderr)


# ---------- чаты и сообщения ----------

def sid(jid):
    return hashlib.sha1(jid.encode()).hexdigest()[:6]


def when(ts):
    return dt.datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")


def period(hours):
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    return {"gte": iso(time.time() - hours * 3600), "lte": iso(time.time() + 86400)}


def mask(s, mk):
    return PHONE.sub(lambda m: mk.label("Телефон", m.group(0)), mk.text(s))


def who(name, mk):
    """Имя как записано в WhatsApp. Номер вместо имени заменяется меткой."""
    name = (name or "").strip()
    m = re.fullmatch(r"\+?(\d{7,15})(@s\.whatsapp\.net)?", name)
    if m:
        return mk.label("Телефон", "+" + m.group(1))
    if name.endswith("@lid"):
        return f"участник {sid(name)}"
    return mask(name, mk) or "без имени"


def text_of(m):
    msg = m.get("message") or {}
    if msg.get("conversation"):
        return msg["conversation"]
    for k, label in KINDS.items():
        if k in msg:
            x = msg[k] or {}
            return f"[{label}] {x.get('caption') or x.get('fileName') or ''}".strip()
    if "reactionMessage" in msg:
        return f"[реакция {(msg['reactionMessage'] or {}).get('text', '')}]"
    return f"[{m.get('messageType') or 'сообщение'}]"


def aliases(keys):
    """Личный чат бывает под скрытым id …@lid и под номером. Сводим оба к …@lid."""
    return {k["remoteJidAlt"]: k["remoteJid"] for k in keys
            if k.get("remoteJid", "").endswith("@lid") and k.get("remoteJidAlt", "").endswith("@s.whatsapp.net")}


def chat_list(where=None):
    rows = api("POST", f"/chat/findChats/{NAME}", {"where": where} if where else {}) or []
    alias = aliases([c["lastMessage"]["key"] for c in rows if c.get("lastMessage")])
    chats = {}
    for c in rows:
        jid = alias.get(c["remoteJid"], c["remoteJid"])
        last = c.get("lastMessage") or {}
        name = c.get("pushName") or (None if (last.get("key") or {}).get("fromMe") else last.get("pushName"))
        cur = chats.setdefault(jid, {"name": None, "last": last, "unread": 0})
        cur["name"] = cur["name"] or name
        cur["unread"] += c.get("unreadCount") or 0
        if int(last.get("messageTimestamp") or 0) > int(cur["last"].get("messageTimestamp") or 0):
            cur["last"] = last
    return chats, alias


def messages(where, limit):
    """Сообщения от новых к старым, не больше limit."""
    out, page = [], 1
    while len(out) < limit:
        r = api("POST", f"/chat/findMessages/{NAME}", {"where": where, "offset": 100, "page": page})["messages"]
        out += r["records"]
        if page >= (r.get("pages") or 0):
            break
        page += 1
    if r["total"] > limit:
        print(f"Показаны последние {limit} из {r['total']} сообщений. Больше: --limit {r['total']}", file=sys.stderr)
    return out[:limit]


def resolve(q, chats, mk, number_ok=False):
    q = q.strip()
    if number_ok and re.fullmatch(NUMBER, q):
        d = re.sub(r"\D", "", q)
        if len(d) == 11 and d[0] == "8":  # 8 701 … так пишут в Казахстане, в WhatsApp это 7 701 …
            d = "7" + d[1:]
        if len(d) == 10:  # 701 … без кода страны
            d = "7" + d
        return d + "@s.whatsapp.net"
    found = [j for j, c in chats.items() if q == sid(j) or q.casefold() in (c["name"] or "").casefold()]
    if len(found) == 1:
        return found[0]
    if not found:
        sys.exit(f"Чат «{q}» не найден. Весь список за месяц: python3 tools/whatsapp.py chats --hours 720")
    lines = "\n".join(f"  [{sid(j)}] {who(chats[j]['name'], mk)}" for j in found[:15])
    sys.exit(f"Под «{q}» подходит несколько чатов, укажите id:\n{lines}")


def cmd_chats(hours):
    if not ready():
        warn_unlinked()
    mk = Masker(load_map())
    chats, _ = chat_list({"messageTimestamp": period(hours)})
    if not chats:
        print(f"За {hours:g} ч сообщений нет.")
    for jid, c in sorted(chats.items(), key=lambda x: -int(x[1]["last"].get("messageTimestamp") or 0)):
        kind = "группа" if jid.endswith("@g.us") else "личный"
        unread = f" · непрочитано {c['unread']}" if c["unread"] else ""
        preview = mask(text_of(c["last"]), mk).replace("\n", " ")[:70]
        print(f"[{sid(jid)}] {who(c['name'], mk)} · {kind} · {when(c['last']['messageTimestamp'])}{unread} · «{preview}»")
    save_map(mk.m)


def cmd_read(chat, hours, limit):
    if not ready():
        warn_unlinked()
    mk = Masker(load_map())
    chats, alias = chat_list()
    where = {"messageTimestamp": period(hours)}
    if chat:
        jid = resolve(chat, chats, mk)
        msgs = messages({**where, "key": {"remoteJid": jid}}, limit)
        other = {m["key"].get("remoteJidAlt") for m in msgs} | {k for k, v in alias.items() if v == jid}
        for j in other - {None, jid}:
            msgs += messages({**where, "key": {"remoteJid": j}}, limit)
    else:
        msgs = messages(where, limit)
    alias.update(aliases([m["key"] for m in msgs]))
    groups = {}
    for m in {m["key"]["id"]: m for m in msgs}.values():
        groups.setdefault(alias.get(m["key"]["remoteJid"], m["key"]["remoteJid"]), []).append(m)
    if not groups:
        print(f"За {hours:g} ч сообщений нет.")
    for jid, ms in sorted(groups.items(), key=lambda g: -max(int(m["messageTimestamp"]) for m in g[1])):
        ms.sort(key=lambda m: int(m["messageTimestamp"]))
        print(f"\n[{sid(jid)}] {who((chats.get(jid) or {}).get('name'), mk)}")
        for m in ms[-limit:]:
            author = "Я" if m["key"].get("fromMe") else who(m.get("pushName"), mk)
            body = mask(text_of(m), mk).replace("\n", "\n      ")
            print(f"  {when(m['messageTimestamp'])} {author}: {body}")
    save_map(mk.m)


def cmd_send(chat, to, text):
    text = (sys.stdin.read() if text == "-" else text).strip()
    if not text:
        sys.exit("Пустой текст, отправлять нечего.")
    ok = ready()
    mk = Masker(load_map())
    chats, _ = chat_list()
    jid = resolve(chat, chats, mk, number_ok=True)
    name = who((chats.get(jid) or {}).get("name") or jid, mk)
    if not re.fullmatch(NUMBER, chat.strip()) and (to or "").strip().casefold() != name.casefold():
        sys.exit(f"Получатель не сверен: [{sid(jid)}] это «{name}». Покажи руководителю, кому уходит сообщение, "
                 f"и повтори с --to \"{name}\".")
    if not ok:
        sys.exit("Телефон не привязан к шлюзу, отправить нельзя. Привяжите: python3 tools/whatsapp.py qr")
    mid = (api("POST", f"/message/sendText/{NAME}", {"number": jid, "text": text}).get("key") or {}).get("id")
    status = "в очереди"
    for _ in range(10):
        time.sleep(1)
        rec = api("POST", f"/chat/findMessages/{NAME}", {"where": {"key": {"id": mid}}, "offset": 1})["messages"]["records"]
        got = {u.get("status") for u in (rec[0].get("MessageUpdate") or [])} if rec else set()
        if got & {"READ", "PLAYED"}:
            status = "прочитано"
            break
        if "DELIVERY_ACK" in got:
            status = "доставлено"
            break
        if "SERVER_ACK" in got:
            status = "сервер WhatsApp принял"
    print(f"Отправлено: {name}. Статус: {status}.")
    save_map(mk.m)


# ---------- команды ----------

def cmd_status():
    if not ENV.exists():
        print("WhatsApp не подключён: шлюз не установлен. Подключите по навыку whatsapp, раздел «Поставить».")
        return 1
    if not alive():
        print("Шлюз установлен и сейчас остановлен. Чтение и отправка запустят его сами и догрузят пропущенное.")
        return 0
    ok = linked()
    if not ok:
        print("Шлюз подключается к WhatsApp, повторите через минуту." if ok is None else
              "Шлюз работает, но телефон не привязан. Привяжите: python3 tools/whatsapp.py qr")
        return 1
    t = total()
    tail = f", последнее {when(t['records'][0]['messageTimestamp'])}" if t["records"] else ""
    print(f"WhatsApp подключён. В базе {t['total']} сообщений{tail}.")
    return 0


def cmd_up():
    if not ENV.exists():
        make_env()
    started = start()
    if not ensure_instance() and started:
        catch_up()
    return cmd_status()


def cmd_qr():
    if not ENV.exists():
        make_env()
    if start():
        catch_up()
    ensure_instance()
    if linked():
        print("Телефон уже привязан.")
        return 0
    print("В телефоне: WhatsApp → Настройки → Связанные устройства → Привязка устройства. Наведите камеру на QR на экране.")
    tmp, shown, n = pathlib.Path(tempfile.mkdtemp(prefix="whatsapp-qr-")), None, 0
    try:
        for _ in range(90):
            if state() == "open":
                print("Телефон привязан. История переписок подтянется за 10–30 минут, телефону в это время нужен интернет.")
                return 0
            r = api("GET", f"/instance/connect/{NAME}") or {}
            if r.get("base64") and r.get("code") != shown:
                n, shown = n + 1, r["code"]
                png = tmp / f"QR-{n}.png"
                png.write_bytes(base64.b64decode(r["base64"].split(",", 1)[-1]))
                run("open", str(png))
                if n > 1:
                    print("QR обновился, на экране новый.")
            time.sleep(2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    api("DELETE", f"/instance/logout/{NAME}", check=False)  # иначе шлюз крутит QR ещё 20 минут и после перезапуска
    sys.exit("За 3 минуты телефон не привязан. Запустите qr ещё раз.")


def cmd_stop():
    if not docker_ready():
        print("Шлюз уже остановлен.")
        return 0
    r = compose("stop")
    if r.returncode:
        sys.exit("Не получилось остановить:\n" + (r.stderr or r.stdout)[-800:])
    print("Шлюз остановлен, переписка в базе сохранена. Автозапуск его не поднимет: шлюз включится при следующем "
          "чтении, отправке или up. Новые сообщения WhatsApp отдаст тогда же, если пауза меньше 30 дней.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="WhatsApp через шлюз на этом Mac")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("status", "up", "qr", "stop"):
        sub.add_parser(c)
    p = sub.add_parser("chats")
    p.add_argument("--hours", type=float, default=24)
    p = sub.add_parser("read")
    p.add_argument("chat", nargs="?")
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("send")
    p.add_argument("chat")
    p.add_argument("text")
    p.add_argument("--to", help="имя получателя, как в chats и read")
    a = ap.parse_args()
    if a.cmd == "chats":
        return cmd_chats(a.hours)
    if a.cmd == "read":
        return cmd_read(a.chat, a.hours, a.limit)
    if a.cmd == "send":
        return cmd_send(a.chat, a.to, a.text)
    return {"status": cmd_status, "up": cmd_up, "qr": cmd_qr, "stop": cmd_stop}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
