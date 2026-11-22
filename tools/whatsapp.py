#!/usr/bin/env python3
"""WhatsApp руководителя через шлюз Evolution API на этом Mac. Сервер не нужен.

Шлюз и база сообщений работают в Docker здесь же, адрес только локальный http://127.0.0.1:18080.
Ключ доступа и пароль базы создаются при первом запуске в .private/whatsapp.env и никуда не уходят.
Пока Mac выключен, WhatsApp держит новые сообщения у себя и отдаёт их шлюзу при следующем запуске.
Чтение и отправка сами запускают остановленный шлюз и догружают пропущенное. С автозапуском шлюз
поднимается при входе в систему и раз в 15 минут проверяет себя: завис — перезапускается.

  python3 tools/whatsapp.py install                  поставить Colima и Docker, поднять шлюз, включить автозапуск
  python3 tools/whatsapp.py status                   установлен ли шлюз и привязан ли телефон
  python3 tools/whatsapp.py up                       поднять шлюз и догрузить пропущенное
  python3 tools/whatsapp.py qr                       привязать телефон по QR
  python3 tools/whatsapp.py stop                     пауза: шлюз стоит, пока его не включат up, чтением или отправкой
  python3 tools/whatsapp.py autostart on|off         автозапуск при входе в систему и проверка раз в 15 минут
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
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import re
import secrets
import shutil
import socket
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
PAUSE = ROOT / ".private" / "whatsapp.paused"  # после stop сторож автозапуска шлюз не поднимает
STARTED = ROOT / ".private" / "whatsapp.started"  # шлюз хоть раз ответил: база создана, перезапуск безопасен
LABEL = "kz.ai-shtab.whatsapp"
PLIST = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = pathlib.Path.home() / "Library" / "Logs" / "ai-shtab" / "whatsapp.log"  # только состояние, без текстов
URL = "http://127.0.0.1:18080"
NAME = "shtab"
NUMBER = r"\+?[\d\s()\-]{10,}"
# Номера, которые не ловит obezlichit.py: 7701…, «7 701 …» без плюса и иностранные с плюсом
PHONE = re.compile(r"(?<![\w+])(?:\+\d[\d\s()\-]{8,16}\d|7[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})(?!\d)")
KINDS = {"imageMessage": "фото", "videoMessage": "видео", "documentMessage": "документ",
         "audioMessage": "голосовое", "stickerMessage": "стикер", "locationMessage": "геолокация",
         "contactMessage": "контакт"}


# ---------- шлюз: запуск, остановка, состояние ----------

def run(*cmd, timeout=120):
    """Команда с пределом по времени: зависшая виртуальная машина не должна вешать сторожа навсегда."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]}: нет ответа за {timeout} с")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: программа не найдена")


def docker_ready():
    return bool(shutil.which("docker")) and run("docker", "info", timeout=20).returncode == 0


def online():
    """Есть ли связь с серверами WhatsApp. Без неё зависанием считать нечего."""
    try:
        socket.create_connection(("web.whatsapp.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


def start_docker():
    if docker_ready():
        return
    if shutil.which("colima"):
        print("Запускаю Colima. В первый раз она скачивает с GitHub образ виртуальной машины около 320 МБ: "
              "на медленном интернете это до часа.")
        # --ssh-config=false: Colima не дописывает свою строку в ~/.ssh/config руководителя
        r = run("colima", "start", "--memory", "3", "--ssh-config=false", timeout=3600)
        if r.returncode:  # прерванную загрузку Colima продолжает с того же места (curl -C -)
            sys.exit("Colima не запустилась:\n" + (r.stderr or r.stdout)[-800:] +
                     "\nПовторите ту же команду: загрузка продолжится с того места, где оборвалась.")
    elif pathlib.Path("/Applications/Docker.app").exists():
        run("open", "-a", "Docker")
    else:
        sys.exit("Нет Docker. Поставьте Colima по навыку whatsapp, раздел «Поставить», и повторите.")
    for _ in range(90):
        if docker_ready():
            return
        time.sleep(2)
    sys.exit("Docker не запустился за 3 минуты. Выполните `colima start` или откройте Docker и посмотрите ошибку.")


def compose(*args, timeout=120):
    base = ["docker", "compose"] if run("docker", "compose", "version").returncode == 0 else ["docker-compose"]
    return run(*base, "-f", str(COMPOSE), *args, timeout=timeout)


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
    if owner():  # был привязан и переподключается: нет интернета или WhatsApp недоступен
        return None
    # ни разу не привязан: если шлюз показывает QR, ждёт привязки. В этом состоянии connect только отдаёт QR
    return False if (api("GET", f"/instance/connect/{NAME}") or {}).get("base64") else None


def owner():
    """Номер, к которому шлюз был привязан. Пусто, если телефон ни разу не привязывали."""
    return next((i.get("ownerJid") for i in api("GET", "/instance/fetchInstances") or [] if i.get("name") == NAME), None)


def since_wake():
    """Секунды с последнего пробуждения Mac."""
    m = re.search(r"sec = (\d+)", run("sysctl", "-n", "kern.waketime", timeout=10).stdout)
    return time.time() - int(m.group(1)) if m else 1e9


def lock(wait):
    """Блокировка на время команды. Сторож не ждёт: если занято, пропускает проверку. Агент ждёт."""
    ENV.parent.mkdir(mode=0o700, exist_ok=True)
    f = open(ENV.parent / "whatsapp.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not wait:
            f.close()
            return None
        print("Шлюз занят другой командой (проверка или запуск), жду, пока закончит.")
        fcntl.flock(f, fcntl.LOCK_EX)
    return f


def make_env():
    ENV.parent.mkdir(mode=0o700, exist_ok=True)
    pw = secrets.token_hex(16)
    ENV.write_text(f"AUTHENTICATION_API_KEY={secrets.token_hex(24)}\nPOSTGRES_PASSWORD={pw}\n"
                   f"DATABASE_CONNECTION_URI=postgresql://evolution:{pw}@db:5432/evolution?schema=evolution_api\n")
    ENV.chmod(0o600)


def start():
    """Поднимает шлюз, если он не отвечает. True, если пришлось поднимать.
    Пока шлюз ни разу не ответил, идёт создание базы: его не перезапускаем, чтобы не сорвать."""
    if alive():
        return False
    start_docker()
    PAUSE.unlink(missing_ok=True)
    print("Поднимаю шлюз WhatsApp. В первый раз скачивается около 500 МБ, это несколько минут.")
    r = compose("up", "-d", timeout=1800)
    if r.returncode:
        sys.exit("Шлюз не поднялся:\n" + (r.stderr or r.stdout)[-1500:])
    for i in range(150):
        if alive():
            STARTED.touch()
            return True
        if i == 90 and STARTED.exists():  # 3 минуты без ответа: перезапустить шлюз один раз
            compose("restart", "evolution")
        time.sleep(2)
    sys.exit("Шлюз не ответил за 5 минут. Покажи этот текст руководителю и запиши в обучение/вопросы_к_созвону.md.")


def clock_drift():
    """Насколько часы виртуальной машины Colima разошлись с часами Mac, в секундах. 0, если Colima не используется."""
    if not shutil.which("colima") or run("colima", "status", timeout=30).returncode:
        return 0
    r = compose("exec", "-T", "evolution", "date", "+%s", timeout=30)
    return abs(int(r.stdout.strip()) - time.time()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0


def heal():
    """Шлюз отвечает, но завис: ушли часы виртуальной машины или связь с WhatsApp не восстанавливается."""
    drift = clock_drift()
    if drift > 300:  # известная ошибка Lima на vz: часы прыгают, лечится только перезапуском
        print(f"Часы виртуальной машины разошлись с Mac на {drift:.0f} с, перезапускаю Colima.")
        run("colima", "restart", timeout=600)
        start()
        catch_up()
    elif not online():
        print("Нет связи с серверами WhatsApp, проверка пропущена.")
    elif linked() is None:
        print("Связь с WhatsApp не восстанавливается, перезапускаю шлюз.")
        compose("restart", "evolution")
        for _ in range(60):
            if alive():
                break
            time.sleep(2)
        catch_up()


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
    free = shutil.disk_usage("/").free / 2 ** 30
    if free < 5:
        print(f"Внимание: на диске Mac свободно {free:.1f} ГБ. Когда место кончится, шлюз и база остановятся.")
    loaded = run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}", timeout=10).returncode == 0
    print("Автозапуск: включён." if loaded else "Автозапуск: выключен. Включить: python3 tools/whatsapp.py autostart on")
    if not alive():
        print("Шлюз на паузе после команды stop. Включить: python3 tools/whatsapp.py up" if PAUSE.exists() else
              "Шлюз установлен и сейчас остановлен. Чтение и отправка запустят его сами и догрузят пропущенное.")
        return 0
    ok = linked()
    if not ok and not online():
        print("Нет связи с серверами WhatsApp: проверьте интернет. Шлюз подключится сам, когда связь появится.")
        return 1
    if not ok:
        print("Шлюз подключается к WhatsApp, повторите через минуту." if ok is None else
              "Шлюз работает, но телефон не привязан. Привяжите: python3 tools/whatsapp.py qr")
        return 1
    t = total()
    tail = f", последнее {when(t['records'][0]['messageTimestamp'])}" if t["records"] else ""
    print(f"WhatsApp подключён. В базе {t['total']} сообщений{tail}.")
    return 0


def cmd_up(auto=False):
    """auto — запуск сторожем автозапуска: паузу не снимает, не установленный шлюз не ставит."""
    if auto:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:  # журнал сторожа не растёт больше мегабайта
            LOG.write_text("")
        print(time.strftime("%d.%m %H:%M"), "проверка")
        if PAUSE.exists() or not ENV.exists():
            return 0
    if not ENV.exists():
        make_env()
    started = start()
    if not ensure_instance() and started:
        catch_up()
    elif not started and since_wake() > 180:  # сразу после сна шлюз ещё переподключается, это не зависание
        heal()
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
    if not owner():  # ни разу не привязан: остановить показ QR, иначе шлюз крутит его ещё 20 минут
        api("DELETE", f"/instance/logout/{NAME}", check=False)  # рабочую привязку так не трогаем
    sys.exit("За 3 минуты телефон не привязан. Запустите qr ещё раз.")


def cmd_stop():
    if not ENV.exists():
        print("Шлюз не установлен.")
        return 0
    PAUSE.touch()  # пауза ставится и тогда, когда Docker уже выключен: иначе сторож поднимет шлюз
    if docker_ready():
        r = compose("stop")
        if r.returncode:
            sys.exit("Не получилось остановить:\n" + (r.stderr or r.stdout)[-800:])
    print("Шлюз на паузе, переписка в базе сохранена. Автозапуск его не поднимет: шлюз включится при следующем "
          "чтении, отправке или up. Новые сообщения WhatsApp отдаст тогда же, если пауза меньше 30 дней.")
    return 0


def cmd_install():
    """Ставит Colima и Docker через Homebrew, если нет Docker, поднимает шлюз и включает автозапуск."""
    have = all(shutil.which(x) for x in ("colima", "docker", "docker-compose"))  # прошлая установка могла оборваться
    if not (have or docker_ready() or pathlib.Path("/Applications/Docker.app").exists()):
        brew = next((b for b in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(b)), None)
        if not brew:
            sys.exit("Нет Homebrew. Руководитель ставит его сам: команда с https://brew.sh в приложении «Терминал», "
                     "пароль Mac вводит он. Потом повторите: python3 tools/whatsapp.py install")
        print("Ставлю Colima и Docker, 2–5 минут.")
        r = run(brew, "install", "colima", "docker", "docker-compose", timeout=1800)
        if r.returncode:
            sys.exit("Установка не прошла:\n" + (r.stderr or r.stdout)[-1500:])
        prof = pathlib.Path.home() / ".zprofile"  # чтобы brew, docker и colima находились и в новых сессиях
        if "brew shellenv" not in (prof.read_text() if prof.exists() else ""):
            with prof.open("a") as f:
                f.write(f'\neval "$({brew} shellenv)"\n')
    rc = cmd_up()
    return cmd_autostart(True) or rc


def cmd_autostart(on):
    """Задача macOS (LaunchAgent): `up --auto` при входе в систему и раз в 15 минут."""
    target = f"gui/{os.getuid()}/{LABEL}"
    run("launchctl", "bootout", target, timeout=30)
    for _ in range(10):  # bootout асинхронный: без ожидания bootstrap падает с ошибкой 5
        if run("launchctl", "print", target, timeout=10).returncode:
            break
        time.sleep(1)
    if not on:
        PLIST.unlink(missing_ok=True)
        print("Автозапуск выключен: шлюз включится при чтении, отправке или up.")
        return 0
    closed = [pathlib.Path.home() / d for d in ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")]
    if any(c == ROOT or c in ROOT.parents for c in closed):  # туда macOS не пускает фоновые задачи
        print("Автозапуск не включён: проект лежит в папке, которую macOS закрывает для фоновых задач "
              "(Рабочий стол, Документы, Загрузки, iCloud). Перенесите проект в ~/ai-shtab и повторите autostart on.")
        return 1
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(pathlib.Path(__file__).resolve()), "up", "--auto"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "StartInterval": 900,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }))
    r = run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST), timeout=30)
    if r.returncode:
        PLIST.unlink(missing_ok=True)
        sys.exit("Автозапуск не включился, повторите autostart on:\n" + (r.stderr or r.stdout)[-500:])
    print("Автозапуск включён: шлюз поднимается при входе в систему и раз в 15 минут проверяет себя. "
          "macOS может показать уведомление о новом фоновом объекте python3: это он.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="WhatsApp через шлюз на этом Mac")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("status", "qr", "stop", "install"):
        sub.add_parser(c)
    sub.add_parser("up").add_argument("--auto", action="store_true", help="запуск сторожем автозапуска")
    sub.add_parser("autostart").add_argument("mode", choices=["on", "off"])
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
    if a.cmd != "status":  # одна команда за раз: сторож не перезапускает шлюз посреди отправки или привязки
        auto = a.cmd == "up" and a.auto
        if auto and not ENV.exists():
            return 0
        held = lock(wait=not auto)  # noqa: F841 — блокировка держится, пока жива переменная
        if held is None:
            print(time.strftime("%d.%m %H:%M"), "проверка пропущена: идёт другая команда шлюза")
            return 0
    if a.cmd == "chats":
        return cmd_chats(a.hours)
    if a.cmd == "read":
        return cmd_read(a.chat, a.hours, a.limit)
    if a.cmd == "send":
        return cmd_send(a.chat, a.to, a.text)
    if a.cmd == "up":
        return cmd_up(a.auto)
    if a.cmd == "autostart":
        return cmd_autostart(a.mode == "on")
    return {"status": cmd_status, "qr": cmd_qr, "stop": cmd_stop, "install": cmd_install}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
