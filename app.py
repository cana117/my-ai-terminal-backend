import os, re, html, base64, gzip, json, time, math, struct, hmac, hashlib, random, asyncio, sqlite3, logging, collections, datetime as dt
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

E = os.environ.get
BOT, ADMIN = E("BOT_TOKEN"), int(E("ADMIN_ID", "0") or 0)
TGAPI = E("TG_API", "https://api.telegram.org").rstrip("/")
ADMIN_TOKEN, PKEY = E("ADMIN_TOKEN", ""), E("POLLINATIONS_KEY", "")
DB, DATAVER = E("DB_PATH", "/tmp/data.db"), int(E("MC_DATA_VERSION", "4671"))
OR, POLLI = "https://openrouter.ai/api/v1", "https://gen.pollinations.ai/v1/chat/completions"
MSK, T0 = ZoneInfo("Europe/Moscow"), time.time()

LOGS, LOGQ = collections.deque(maxlen=300), collections.deque(maxlen=1000)
class _H(logging.Handler):
    def emit(self, r):
        line = f"{dt.datetime.now(MSK):%d.%m %H:%M:%S} {r.levelname} {r.getMessage()}"; LOGS.append(line); LOGQ.append(line)
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе токен бота попадает в логи
log = logging.getLogger("myai"); log.addHandler(_H())

db = sqlite3.connect(DB, check_same_thread=False)
SCHEMA = """create table if not exists users(id integer primary key,name text,banned int default 0,joined text);
create table if not exists keys(owner int,key text unique);
create table if not exists cmds(name text primary key,text text);
create table if not exists hist(id integer primary key autoincrement,sid text,role text,content text);
create table if not exists links(cid text primary key,uid int);
create table if not exists sources(id integer primary key autoincrement,owner int,kind text,url text,title text,unique(owner,url));
create table if not exists subs(uid integer primary key,active int default 0,times text default '09:00,21:00',fmt text default 'ai');
create table if not exists posts(id integer primary key autoincrement,at text,text text,sent int default 0);
create table if not exists seen(uid int,key text,primary key(uid,key));
create table if not exists inv(name text primary key,qty int);
create table if not exists settings(k text primary key,v text);
create table if not exists accs(nick text primary key);
create table if not exists logs(id integer primary key autoincrement,line text);"""
db.executescript(SCHEMA)
def flush_logs():
    try:
        while LOGQ: db.execute("insert into logs(line) values(?)", (LOGQ.popleft(),))
        db.execute("delete from logs where id not in (select id from logs order by id desc limit 1000)"); db.commit()
    except Exception: pass

dirty = False
def q(sql, *a):
    global dirty
    c = db.execute(sql, a); db.commit()
    if not sql.lstrip().lower().startswith("select") and "hist" not in sql and "logs" not in sql: dirty = True
    return c.fetchall()

STAT = {"cache": 0, "llm": 0, "bot": "not started", "err": ""}
cool, cache, hist_pref, pending, ipc = {}, collections.OrderedDict(), {}, {}, {}
FREE, VISION = [], []

SYS = {
 "chat": "Ты MY_AI, ассистент в Telegram и на сайте. Стиль: лаконичный терминальный, можно метки в квадратных скобках. "
         "Отвечай на языке пользователя, коротко и по делу. Не предлагай взлом, обход защиты и подобное. "
         "Если приходится остановиться из-за лимита, выдай отчёт: [COMPLETED], [FILE GENERATED], [LIMIT REACHED PREEMPTIVELY], [NEXT STEPS].",
 "vision": "Ты MY_AI. Тебе присылают изображения. Опиши и разбери то, что видишь, по запросу пользователя. Отвечай на его языке, коротко и по делу.",
 "coder": "Ты Агент-Кодер: пишешь и дорабатываешь код, отвечаешь кратко, код в блоках. Без воды.",
 "optimizer": "Ты Агент-Оптимизатор: сжимаешь текст без потери фактов, максимально коротко.",
}
PREF = {"coder": ("coder", "qwen", "deepseek", "devstral"),
        "optimizer": ("8b", "7b", "9b", "mini", "small", "flash", "nano", "lite", "gemma"), "chat": ()}
CODE = re.compile(r"```|код|функци|скрипт|баг|ошибк|python|javascript|html|css|sql|def |class |import ", re.I)
def route(t): return "coder" if CODE.search(t) else "chat"

# ---------- Free models + key pool ----------
async def refresh_models():
    global FREE, VISION
    try:
        async with httpx.AsyncClient(timeout=30) as c: d = (await c.get(OR + "/models")).json()["data"]
        FREE = [m["id"] for m in sorted((m for m in d if m["id"].endswith(":free")), key=lambda m: -(m.get("context_length") or 0))]
        VISION = [m["id"] for m in sorted((m for m in d if m["id"].endswith(":free") and "image" in ((m.get("architecture") or {}).get("input_modalities") or [])),
                                          key=lambda m: -(m.get("context_length") or 0))]
        log.info("free models: %d, with vision: %d", len(FREE), len(VISION))
    except Exception as e: log.error("models: %r", e)

def keys_for(uid):
    env = [k.strip() for k in E("OPENROUTER_KEYS", "").split(",") if k.strip()]
    return env + [k for (k,) in q("select key from keys where owner in (0,?)", uid)]

def models_for(role):
    ms = [m for m in (VISION if role == "vision" else FREE) if cool.get(m, 0) <= time.time()]
    p = [m for m in ms if any(x in m.lower() for x in PREF.get(role, ()))]
    return (p + [m for m in ms if m not in p])[:4]

def reset_at(r):
    h = r.headers
    try:
        if "retry-after" in h: return time.time() + float(h["retry-after"])
        if "x-ratelimit-reset" in h: return float(h["x-ratelimit-reset"]) / 1000
    except ValueError: pass
    return time.time() + 3600

async def llm(msgs, role="chat", uid=0):
    msgs = [{"role": "system", "content": SYS[role]}] + msgs; now = time.time(); STAT["llm"] += 1
    async with httpx.AsyncClient(timeout=60) as c:
        for m in models_for(role):
            for k in keys_for(uid):
                if cool.get(k, 0) > now: continue
                try: r = await c.post(OR + "/chat/completions", headers={"Authorization": "Bearer " + k}, json={"model": m, "messages": msgs})
                except httpx.HTTPError: break
                if r.status_code == 200:
                    try:
                        t = r.json()["choices"][0]["message"]["content"]
                        if t: return t
                    except Exception: pass
                    break
                if r.status_code == 429:
                    if "per-day" in r.text: cool[k] = reset_at(r); continue
                    cool[m] = now + 90; break
                if r.status_code in (401, 402, 403): cool[k] = now + 3600; continue
                break
        try:
            r = await c.post(POLLI, headers={"Authorization": "Bearer " + PKEY} if PKEY else {}, json={"model": "openai", "messages": msgs})
            if r.status_code == 200: return r.json()["choices"][0]["message"]["content"]
            log.error("pollinations %s", r.status_code)
        except Exception as e: log.error("pollinations %r", e)
    ts = [v - now for v in cool.values() if v > now]
    return f"[RATE LIMIT REACHED]: Лимит исчерпан. Восстановление через {max(1, math.ceil(min(ts, default=60) / 60))} минут."

# ---------- Agent-Optimizer: история, сжатие, кэш ----------
def hload(sid): return [{"role": r, "content": c} for r, c in q("select role,content from hist where sid=? order by id desc limit 30", sid)][::-1]
def hadd(sid, role, text):
    q("insert into hist(sid,role,content) values(?,?,?)", sid, role, text)
    q("delete from hist where sid=? and id not in (select id from hist where sid=? order by id desc limit 40)", sid, sid)

async def compress(sid, h, uid):
    if sum(len(x["content"]) for x in h) <= 6000: return h
    old, new = h[:-6], h[-6:]
    s = await llm([{"role": "user", "content": "Сожми диалог до 800 символов:\n" + "\n".join(f"{x['role']}: {x['content'][:600]}" for x in old)}], "optimizer", uid)
    if s.startswith("[RATE LIMIT"): s = " | ".join(x["content"][:80] for x in old)[:800]
    q("delete from hist where sid=?", sid)
    for x in [{"role": "user", "content": "Выжимка прошлого: " + s}] + new: hadd(sid, x["role"], x["content"])
    return hload(sid)

async def reply(sid, uid, text, role=None):
    role = role or route(text); ck = None
    h = await compress(sid, hload(sid), uid)
    if not h and len(text) < 200:
        ck = hashlib.sha1((role + text.lower().strip()).encode()).hexdigest()
        if ck in cache and cache[ck][0] > time.time():
            STAT["cache"] += 1; out = cache[ck][1]; hadd(sid, "user", text); hadd(sid, "assistant", out); return out
    out = await llm(h + [{"role": "user", "content": text}], role, uid)
    if not out.startswith("[RATE LIMIT"):
        hadd(sid, "user", text); hadd(sid, "assistant", out)
        if ck: cache[ck] = (time.time() + 21600, out); cache.move_to_end(ck); [cache.popitem(False) for _ in range(max(0, len(cache) - 500))]
    return out

# ---------- Minecraft .schem (Sponge v2) ----------
BLK = re.compile(r"^[a-z0-9_]+:[a-z0-9_/]+(\[[a-z0-9_=,]+\])?$")
SPEC = ('Верни ТОЛЬКО JSON без markdown: {"size":[X,Y,Z],"ops":[{"op":"fill","block":"minecraft:stone_bricks","from":[0,0,0],"to":[9,0,9]},'
        '{"op":"set","block":"minecraft:oak_door[half=lower]","pos":[4,1,0]}]}. size не больше 48 по оси, координаты от 0, до 150 операций, '
        'только блоки Minecraft 1.21. Постройка: ')
def _s(s): b = s.encode(); return struct.pack(">H", len(b)) + b
def _t(t, n, p): return bytes([t]) + _s(n) + p

def build_schem(spec):
    dim = [int(v) for v in spec["size"]]; W, H, L = dim
    assert all(1 <= v <= 48 for v in dim), "размер 1..48"
    grid = {}
    for o in spec["ops"][:150]:
        b = o["block"]; assert BLK.match(b), "блок " + b
        if o["op"] == "fill":
            a, z = o["from"], o["to"]
            rg = [range(max(0, min(a[i], z[i])), min(dim[i] - 1, max(a[i], z[i])) + 1) for i in range(3)]
            for x in rg[0]:
                for y in rg[1]:
                    for zz in rg[2]: grid[(x, y, zz)] = b
        elif o["op"] == "set":
            p = tuple(o["pos"])
            if all(0 <= p[i] < dim[i] for i in range(3)): grid[p] = b
    pal, data = {"minecraft:air": 0}, bytearray()
    for y in range(H):
        for z in range(L):
            for x in range(W):
                i = pal.setdefault(grid.get((x, y, z), "minecraft:air"), len(pal))
                while True:
                    bits = i & 0x7F; i >>= 7; data.append(bits | 0x80 if i else bits)
                    if not i: break
    f = [_t(3, "Version", struct.pack(">i", 2)), _t(3, "DataVersion", struct.pack(">i", DATAVER)),
         _t(2, "Width", struct.pack(">h", W)), _t(2, "Height", struct.pack(">h", H)), _t(2, "Length", struct.pack(">h", L)),
         _t(3, "PaletteMax", struct.pack(">i", len(pal))),
         _t(10, "Palette", b"".join(_t(3, k, struct.pack(">i", v)) for k, v in pal.items()) + b"\0"),
         _t(7, "BlockData", struct.pack(">i", len(data)) + bytes(data)), _t(11, "Offset", struct.pack(">4i", 3, 0, 0, 0))]
    return gzip.compress(_t(10, "Schematic", b"".join(f) + b"\0")), len(grid), len(pal)

async def mc(uid, desc):
    raw = await llm([{"role": "user", "content": SPEC + desc}], "coder", uid)
    if raw.startswith("[RATE LIMIT"): return raw, None
    try:
        m = re.search(r"\{.*\}", raw, re.S); blob, n, p = build_schem(json.loads(m.group(0)))
        return f"[MC OK] блоков: {n}, типов: {p}. Вставка: //schem load, //paste", blob
    except Exception as e: return f"[MC ERROR] модель вернула негодную схему ({e}). Повтори запрос.", None

# ---------- PS99 ----------
rap_prev = {}
PT = {0: "", 1: " Golden", 2: " Rainbow"}
def rap_label(x):
    c, cat = x.get("configData") or {}, x.get("category", "")
    name = str(c.get("id", "?")) + PT.get(c.get("pt", 0), f" pt{c.get('pt')}")
    if c.get("sh"): name += " Shiny"
    if c.get("tn"): name += f" T{c['tn']}"
    return name if cat == "Pet" else f"{name} [{cat}]"

async def api_get(path):
    async with httpx.AsyncClient(timeout=40) as c: return (await c.get("https://ps99.biggamesapi.io" + path)).json()
async def rap_load(): return {rap_label(x): x.get("value", 0) for x in (await api_get("/api/rap"))["data"]}
async def exists_load(): return {rap_label(x): x.get("value", 0) for x in (await api_get("/api/exists"))["data"]}

def short(v): return f"{v / 1e12:.2f}T" if v >= 1e12 else f"{v / 1e9:.1f}B" if v >= 1e9 else f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v:,}"

async def ps99():
    try: d = await rap_load()
    except Exception as e: return f"[PS99 ERROR] API недоступен: {e!r}"
    top = sorted(d.items(), key=lambda x: -x[1])[:10]
    t = "\n".join(f"{k}: {short(v)}" for k, v in top)
    return "[PS99 RAP TOP-10]\n" + t + "\n\n" + await llm([{"role": "user", "content": "Единицы: T = триллион, B = миллиард. Не меняй единицы и не выдумывай цифры. Дай 2 коротких вывода по рынку PS99:\n" + t}], "chat")

async def inv_report(nick):
    try:
        d = await api_get(f"/v1/players/{nick}?include=inventory")
        if d.get("status") != "ok": return f"[{nick}] не найден или нет публичных данных. Привяжи Roblox на db.biggames.io и открой инвентарь.", None
        v = (d["data"].get("views") or {}).get("inventory") or {}
        if not v.get("available"): return f"[{nick}] инвентарь скрыт ({v.get('reason', '?')}). Открой его в настройках на db.biggames.io.", None
        items = v["data"]["items"]
    except Exception as e: return f"[{nick}] [ERROR] {e!r}", None
    rows = sorted(((i.get("displayName") or i.get("id"), i.get("variant"), i.get("count", 1), (i.get("rap") or 0) * (i.get("count") or 1)) for i in items), key=lambda x: -x[3])
    tot = sum(x[3] for x in rows)
    return f"[{nick}] RAP: {short(tot)}, предметов: {len(items)}\n" + "\n".join(f"{n}{' ' + vr if vr else ''} x{c}: {short(val)}" for n, vr, c, val in rows[:12]), tot

NEWCOLS = ("Pets", "Eggs", "Hoverboards", "Booths", "Lootboxes", "Enchants", "Currency", "MiscItems")
async def ps99_new():
    out = []
    for col in NEWCOLS:
        try: names = {x.get("configName") for x in (await api_get("/api/collection/" + col))["data"] if x.get("configName")}
        except Exception: continue
        old = q("select v from settings where k=?", "col:" + col); prev = set(json.loads(old[0][0])) if old else None
        if prev is not None and names - prev:
            new = sorted(names - prev); out.append(f"{col}: " + ", ".join(new[:15]) + (f" (+{len(new) - 15})" if len(new) > 15 else ""))
        if prev is None or names != prev: q("insert or replace into settings values(?,?)", "col:" + col, json.dumps(sorted(names)))
    if out: await say(ADMIN, "[PS99 НОВОЕ]\n" + "\n".join(out))

async def ps99_watch():
    for (n,) in q("select nick from accs"):
        _, tot = await inv_report(n)
        if tot is None: continue
        old = q("select v from settings where k=?", "invtot:" + n)
        if not old or old[0][0] != str(tot):
            q("insert or replace into settings values(?,?)", "invtot:" + n, str(tot))
            if old and float(old[0][0]) > 0 and abs(tot - float(old[0][0])) / float(old[0][0]) > 0.02: await say(ADMIN, f"[INV] {n}: {short(int(float(old[0][0])))} -> {short(tot)}")
    if dt.datetime.now(MSK).hour % 3 == 0: await ps99_new()

async def ps99_movers():
    global rap_prev
    try: d = await rap_load()
    except Exception: return
    if rap_prev:
        mv = [(k, rap_prev[k], v) for k, v in d.items() if rap_prev.get(k, 0) > 1e6 and abs(v - rap_prev[k]) / rap_prev[k] > 0.25]
        if mv: await say(ADMIN, "[PS99 MOVERS]\n" + "\n".join(f"{k}: {a:,} -> {b:,}" for k, a, b in mv[:10]))
    rap_prev = d

async def digest():
    async with httpx.AsyncClient(timeout=30) as c:
        try: xml = (await c.get(E("NEWS_RSS", "https://lenta.ru/rss/news"))).text
        except Exception: return "[NEWS ERROR] лента недоступна"
    t = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml)[1:9]
    return await llm([{"role": "user", "content": "Сделай короткую сводку:\n" + "\n".join(t)}], "optimizer")

VERSION = "1.7"
CHANGELOG = ["1.7: фото, живой инвентарь PS99, исправлены цены, слежение за новым",
             "1.6: до 5 копий базы в двух местах, автовыбор свежей и запасной, логи сохраняются",
             "1.5: отдельный бот для бэкапов (/setbackup, /admin bots)",
             "1.4: база сама восстанавливается после перезапуска",
             "1.3: объявления от админа всем (сразу или по расписанию)",
             "1.2: новости из твоих каналов (RSS, YouTube, Telegram), расписание /time, /sub, /format",
             "1.1: несколько ИИ-агентов, экономия токенов, общая история веб + бот"]

# ---------- Новости: источники, расписание ----------
def clean(t): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(t))).strip()
def kind_of(s):
    if "youtube.com" in s or "youtu.be" in s or re.fullmatch(r"UC[\w-]{22}", s): return "yt"
    return "tg" if s.startswith("@") or "t.me/" in s else "rss"

def parse_rss(x):
    out = []
    for seg in re.split(r"<item[ >]|<entry[ >]", x)[1:9]:
        t = re.search(r"<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", seg, re.S)
        l = re.search(r'<link[^>]*href="([^"]+)"', seg) or re.search(r"<link>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link>", seg, re.S)
        if t and l: out.append((l.group(1).strip(), clean(t.group(1))[:200], l.group(1).strip()))
    return out

def parse_tg(h):
    out = []
    for seg in h.split('data-post="')[1:][-8:]:
        post = seg.split('"')[0]; m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', seg, re.S)
        if m and clean(m.group(1)): out.append((post, clean(m.group(1))[:200], "https://t.me/" + post))
    return out[::-1]

async def norm_source(s):
    k = kind_of(s)
    if k == "tg":
        n = re.sub(r"^.*t\.me/(s/)?", "", s).lstrip("@").split("/")[0]
        if not re.fullmatch(r"\w{4,}", n): raise ValueError("не понял имя канала")
        return k, n
    if k == "yt":
        m = re.search(r"UC[\w-]{22}", s)
        if m: return k, m.group(0)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            h = (await c.get(s if s.startswith("http") else "https://www.youtube.com/" + s)).text
        m = re.search(r'"channelId":"(UC[\w-]{22})"', h)
        if not m: raise ValueError("YouTube-канал не найден")
        return k, m.group(1)
    if not s.startswith("http"): raise ValueError("нужна ссылка на RSS, @канал или YouTube")
    return k, s

srccache = {}
async def fetch_items(kind, url):
    ck = (kind, url)
    if ck in srccache and srccache[ck][0] > time.time(): return srccache[ck][1]
    items = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            if kind == "tg": items = parse_tg((await c.get("https://t.me/s/" + url)).text)
            elif kind == "yt": items = parse_rss((await c.get("https://www.youtube.com/feeds/videos.xml?channel_id=" + url)).text)
            else: items = parse_rss((await c.get(url)).text)
    except Exception as e: log.error("source %s: %r", url[:40], e)
    srccache[ck] = (time.time() + 300, items); return items

async def collect(uid, only_new):
    out = []
    for kind, url, title in q("select kind,url,title from sources where owner in (0,?)", uid):
        for key, t, l in (await fetch_items(kind, url))[:5]:
            if only_new and q("select 1 from seen where uid=? and key=?", uid, key): continue
            out.append((title or url, key, t, l))
    return out

def get_sub(uid): return (q("select active,times,fmt from subs where uid=?", uid) or [(0, "09:00,21:00", "ai")])[0]

async def fmt_digest(uid, items, fmt):
    items = items[:15]; head = ""
    if fmt == "ai":
        head = await llm([{"role": "user", "content": "Коротко, 2-3 предложения: о чём эти новости:\n" + "\n".join(i[2] for i in items)}], "optimizer", uid)
        head = "" if head.startswith("[RATE LIMIT") else head + "\n\n"
    return head + "\n\n".join(f"• [{s}] {t}\n{l}" for s, _, t, l in items)

async def job_digests(hhmm):
    for uid, times, fmt in q("select uid,times,fmt from subs where active=1"):
        if hhmm not in times.split(","): continue
        try:
            items = await collect(uid, True)
            if items:
                await say(uid, "[NEWS]\n" + await fmt_digest(uid, items, fmt), disable_web_page_preview=True)
                for _, k, _, _ in items: q("insert or ignore into seen values(?,?)", uid, k)
        except Exception as e: log.error("digest %s: %r", uid, e)
        await asyncio.sleep(0.1)

# ---------- Объявления от админа всем ----------
def parse_when(arg):
    n = dt.datetime.now(MSK)
    m = re.match(r"^(\d{1,2})\.(\d{1,2}) ([01]?\d|2[0-3]):([0-5]\d) (.+)$", arg, re.S)
    if m:
        d, mo, h, mi, t = m.groups(); w = n.replace(month=int(mo), day=int(d), hour=int(h), minute=int(mi), second=0, microsecond=0)
        return (w.replace(year=n.year + 1) if w < n else w), t
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d) (.+)$", arg, re.S)
    if m:
        h, mi, t = m.groups(); w = n.replace(hour=int(h), minute=int(mi), second=0, microsecond=0)
        return (w + dt.timedelta(days=1) if w <= n else w), t
    return n, arg

async def job_posts(_=None):
    now = dt.datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    for pid, text in q("select id,text from posts where sent=0 and at<=?", now):
        q("update posts set sent=1 where id=?", pid); n = 0
        for (i,) in q("select id from users where banned=0"):
            try: await say(i, "[MY_AI] " + text); n += 1
            except Exception: pass
            await asyncio.sleep(0.05)
        await say(ADMIN, f"[POST #{pid}] доставлено: {n}")

# ---------- Telegram ----------
async def tg(method, token=None, **kw):
    async with httpx.AsyncClient(timeout=70) as c: return (await c.post(f"{TGAPI}/bot{token or BOT}/{method}", **kw)).json()
async def say(chat, text, token=None, **kw):
    for i in range(0, max(len(text), 1), 4000): await tg("sendMessage", token, json={"chat_id": chat, "text": text[i:i + 4000], **kw})

HELP = ("[MY_AI] Просто пиши, отвечу.\n\nНовости:\n/news - свежее из источников\n/sources - список\n/addsource ссылка - добавить (RSS, @канал Telegram, YouTube)\n"
        "/delsource N - удалить\n/sub on|off - рассылка\n/time 09:00 21:00 - когда слать (МСК)\n/format ai|short - с кратким пересказом или только заголовки\n/updates - что нового\n\n"
        "Фото: пришли картинку с подписью-вопросом, разберу.\n/nick имя1 имя2 - свободны ли ники Minecraft.\nДругое: /key add|list|del - свой ключ OpenRouter, /agent coder|chat|auto, /link, /clear, /migrate_chat\n"
        "Ключ: openrouter.ai -> Keys -> Create Key, затем /key add sk-or-...")
HELP_ADMIN = "\n\nАдмин: /admin, /mc описание, /news ps99\n/post текст - всем сразу; /post 18:30 текст; /post 05.11 18:30 текст\n/posts, /delpost N\n/acc add НикRoblox - привязать аккаунт PS99, /inv - живой инвентарь с RAP\n/price имя - цены всех вариантов, /new - что нового в игре\n/admin backup - новая копия сейчас, /admin backups - список копий, /admin restore yes - восстановить, /admin bots - проверка ботов\n/setbackup ТОКЕН - привязать бота бэкапов\n/addsource all ссылка - общий источник (без all - только твой)"
ADMIN_SUBS = {"users", "ban", "unban", "viewkeys", "createcmd", "keypool", "status", "backup", "bots", "backups", "restore", "addkey", "logs", "broadcast", "health", "agents", "cachecl"}
MENU = {"inline_keyboard": [[{"text": "Новости", "callback_data": "/news"}, {"text": "Рассылка вкл", "callback_data": "/sub on"}],
                             [{"text": "Источники", "callback_data": "/sources"}, {"text": "Справка", "callback_data": "/help"}]]}
def mask(k): return k[:8] + "..." + k[-4:]

async def admin(sub, arg):
    if sub == "users": return "\n".join(f"{i} {n} {'BAN' if b else ''}" for i, n, b in q("select id,name,banned from users")) or "пусто"
    if sub in ("ban", "unban") and arg.isdigit(): q("update users set banned=? where id=?", int(sub == "ban"), int(arg)); return f"[{sub.upper()}] {arg}"
    if sub == "addkey" and arg: q("insert or ignore into keys values(0,?)", arg.strip()); return "[OK] ключ в общем пуле"
    if sub == "viewkeys": return "\n".join(f"{o}: {mask(k)}" for o, k in q("select owner,key from keys")) or "пусто (env-ключи не показываются)"
    if sub == "keypool":
        now = time.time(); ks = keys_for(0)
        return f"пул: {len(ks)}\n" + "\n".join(f"{mask(k)} {'cooldown %dс' % (cool[k] - now) if cool.get(k, 0) > now else 'OK'}" for k in ks)
    if sub == "createcmd" and " " in arg: n, _, t = arg.partition(" "); q("insert or replace into cmds values(?,?)", n.lstrip("/").lower(), t); return "[OK] /" + n.lstrip("/")
    if sub == "logs": flush_logs(); return "\n".join(l for (l,) in q("select line from logs order by id desc limit ?", int(arg) if arg.isdigit() else 25)[::-1]) or "логи пусты"
    if sub == "backup":
        err = await job_backup(force=True)
        return "[OK] бэкап отправлен и закреплён" if not err else f"[BACKUP ERROR] {err}\nОткрой бота бэкапов и нажми /start, потом снова /admin backup"
    if sub == "backups":
        c = await bk_scan(); return "\n".join(f"{i}. {l}" for i, (_, _, _, l) in enumerate(c, 1)) or "бэкапов нет"
    if sub == "restore":
        if arg != "yes": return "Заменит текущую базу самой свежей рабочей копией. Подтверди: /admin restore yes"
        return "[RESTORE] " + await restore(force=True)
    if sub == "bots":
        async def chk(t):
            try: me = await tg("getMe", t); return "@" + me["result"]["username"] if me.get("ok") else "токен НЕ РАБОТАЕТ"
            except Exception: return "Telegram недоступен"
        t, c = bk_target()
        return ("Чат-бот: " + (await chk(BOT) if BOT else "BOT_TOKEN не задан") + "\nБот бэкапов: "
                + ("не привязан, копии идут в чат-бот" if t == BOT else await chk(t) + f" (чат {c})") + "\nБаза: " + ("есть изменения" if dirty else "бэкап свежий"))
    if sub == "cachecl": cache.clear(); return "[OK] кэш очищен"
    if sub == "agents": return "\n".join(f"{r}: {', '.join(models_for(r)) or 'нет free-моделей -> Pollinations'}" for r in ("coder", "optimizer", "chat"))
    if sub == "broadcast" and arg:
        n = 0
        for (i,) in q("select id from users where banned=0"):
            try: await say(i, "[BROADCAST] " + arg); n += 1
            except Exception: pass
            await asyncio.sleep(0.05)
        return f"[BROADCAST] доставлено {n}"
    up = int(time.time() - T0)
    return (f"[STATUS] up={up // 3600}ч{up % 3600 // 60}м users={len(q('select id from users'))} keys={len(keys_for(0))} free_models={len(FREE)} "
            f"llm_calls={STAT['llm']} cache_hits={STAT['cache']} bot={STAT['bot']} err={STAT['err'][:80]}\n"
            "Команды: users, ban/unban ID, broadcast ТЕКСТ, logs [N], agents, cachecl, keypool, viewkeys, addkey, createcmd, health")

def pick_photo(m):
    if m.get("photo"):
        ph = [p for p in m["photo"] if p.get("file_size", 0) <= 3_000_000] or m["photo"][:1]
        return ph[-1]["file_id"]
    d = m.get("document") or {}
    if d.get("mime_type", "").startswith("image/") and d.get("file_size", 0) <= 5_000_000: return d["file_id"]

async def mc_free(name):
    if not (name.isascii() and re.fullmatch(r"\w{3,16}", name)): return "неверный формат (3-16 символов: буквы, цифры, _)"
    try:
        async with httpx.AsyncClient(timeout=15) as c: r = await c.get("https://api.minecraftservices.com/minecraft/profile/lookup/name/" + name)
    except Exception: return "не удалось проверить"
    return "занят" if r.status_code == 200 else "свободен (по данным Mojang)" if r.status_code == 404 else f"не знаю (код {r.status_code})"

async def handle_photo(m, uid):
    cap = (m.get("caption") or "").strip() or "Опиши, что на изображении, и ответь по делу."
    await tg("sendChatAction", json={"chat_id": uid, "action": "typing"})
    info = (await tg("getFile", json={"file_id": pick_photo(m)})).get("result") or {}
    if not info.get("file_path"): return await say(uid, "[ERROR] не смог скачать фото (возможно, слишком большое)")
    async with httpx.AsyncClient(timeout=60) as c: blob = (await c.get(f"{TGAPI}/file/bot{BOT}/{info['file_path']}")).content
    mime = "image/png" if info["file_path"].lower().endswith(".png") else "image/jpeg"
    sid = f"tg:{uid}"
    out = await llm(hload(sid)[-6:] + [{"role": "user", "content": [{"type": "text", "text": cap},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(blob).decode()}}]}], "vision", uid)
    if not out.startswith("[RATE LIMIT"): hadd(sid, "user", "[фото] " + cap); hadd(sid, "assistant", out)
    await say(uid, out)

async def handle(m):
    u = m["from"]; uid = u["id"]; txt = (m.get("text") or "").strip()
    if not txt and not pick_photo(m): return
    row = q("select banned from users where id=?", uid)
    if not row:
        q("insert into users values(?,?,0,?)", uid, u.get("first_name", ""), dt.datetime.now().isoformat())
        await say(ADMIN, f"[NEW USER] {uid} {u.get('first_name', '')}")
    elif row[0][0]: return
    if not txt: return await handle_photo(m, uid)
    cmd, _, arg = txt.partition(" "); cmd = cmd.split("@")[0].lower(); arg = arg.strip(); sid = f"tg:{uid}"
    r = lambda s, **kw: say(uid, s, **kw)
    if cmd == "/start": return await r("[MY_AI // TERMINAL READY] Выбери модуль или просто пиши:", reply_markup=MENU)
    if cmd == "/help": return await r(HELP + (HELP_ADMIN if uid == ADMIN else ""))
    if cmd == "/clear": q("delete from hist where sid=?", sid); return await r("[OK] чат очищен")
    if cmd == "/news":
        if arg == "ps99" and uid == ADMIN: return await r(await ps99())
        items = await collect(uid, False)
        return await r(await fmt_digest(uid, items, get_sub(uid)[2]) if items else "Источников нет или они пусты. /addsource ссылка", disable_web_page_preview=True)
    if cmd == "/sources":
        rows = q("select id,kind,title,owner from sources where owner in (0,?)", uid)
        return await r("\n".join(f"{i}. [{k}] {t} {'(общий)' if o == 0 else ''}" for i, k, t, o in rows) or "пусто. /addsource ссылка")
    if cmd == "/addsource":
        if not arg: return await r("Пришли ссылку: RSS, @канал Telegram или YouTube. Пример: /addsource @durov")
        own = uid
        if uid == ADMIN and arg.lower().startswith("all "): own, arg = 0, arg[4:].strip()
        if uid != ADMIN and q("select count(*) from sources where owner=?", uid)[0][0] >= 5: return await r("Лимит 5 своих источников. /delsource N")
        try:
            k, u = await norm_source(arg); its = await fetch_items(k, u)
        except Exception as e: return await r(f"[ERROR] {e}")
        if not its: return await r("[ERROR] по этой ссылке новостей не нашёл")
        q("insert or ignore into sources(owner,kind,url,title) values(?,?,?,?)", own, k, u, arg[:40])
        return await r(f"[OK] добавлен ({k}), новостей найдено: {len(its)}" + (" (общий, виден всем)" if own == 0 else (" (только твой)" if uid == ADMIN else "")))
    if cmd == "/delsource" and arg.isdigit():
        q("delete from sources where id=? and (owner=? or ?)", int(arg), uid, uid == ADMIN); return await r("[OK] удалено, если это был твой источник")
    if cmd in ("/sub", "/time", "/format"):
        q("insert or ignore into subs(uid) values(?)", uid)
        if cmd == "/sub" and arg in ("on", "off"): q("update subs set active=? where uid=?", int(arg == "on"), uid)
        if cmd == "/format" and arg in ("ai", "short"): q("update subs set fmt=? where uid=?", arg, uid)
        if cmd == "/time":
            ts = [x for x in arg.split() if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x)][:4]
            if not ts: return await r("Пример: /time 09:00 21:00")
            q("update subs set times=? where uid=?", ",".join(ts), uid)
        a, t, f = get_sub(uid); return await r(f"[РАССЫЛКА] {'ВКЛ' if a else 'ВЫКЛ'}, время: {t} (МСК), формат: {f}. Приходит только новое.")
    if cmd == "/updates":
        a, t, f = get_sub(uid); return await r(f"[v{VERSION}]\n" + "\n".join(CHANGELOG) + f"\n\nТвоя рассылка: {'ВКЛ' if a else 'ВЫКЛ'}, {t} МСК")
    if uid == ADMIN and cmd == "/post":
        if not arg: return await r("Всем сразу: /post текст\nНа время: /post 18:30 текст\nНа дату: /post 05.11 18:30 текст")
        try: w, t = parse_when(arg)
        except ValueError: return await r("[ERROR] неверная дата")
        q("insert into posts(at,text) values(?,?)", w.strftime("%Y-%m-%d %H:%M"), t)
        asyncio.create_task(job_posts())
        return await r(f"[OK] запланировано на {w:%d.%m %H:%M} МСК" if w > dt.datetime.now(MSK) else "[OK] отправляю всем")
    if uid == ADMIN and cmd == "/posts":
        return await r("\n".join(f"{i}. {a} - {t[:60]}" for i, a, t in q("select id,at,text from posts where sent=0 order by at")) or "запланированных нет")
    if uid == ADMIN and cmd == "/delpost" and arg.isdigit():
        q("delete from posts where id=? and sent=0", int(arg)); return await r("[OK] удалено")
    if uid == ADMIN and cmd == "/setbackup":
        if m.get("message_id"): await tg("deleteMessage", json={"chat_id": uid, "message_id": m["message_id"]})
        if not arg: return await r("Пришли: /setbackup ТОКЕН_БОТА_БЭКАПОВ (сообщение удалю). Сначала открой этого бота и нажми /start.")
        me = await tg("getMe", arg)
        if not me.get("ok"): return await r("[ERROR] токен не подошёл")
        q("insert or replace into settings values('backup_token',?)", arg)
        err = await job_backup(force=True)
        return await r(f"[OK] бот бэкапов @{me['result']['username']} привязан. " + (f"\n[BACKUP ERROR] {err}\nОткрой его, нажми /start, потом /admin backup" if err else "Тестовый бэкап отправлен.")
                       + "\nЧтобы это пережило перезапуск, добавь токен в Render как BACKUP_BOT_TOKEN.")
    if cmd == "/nick":
        names = re.findall(r"\S+", arg)[:15]
        if not names: return await r("Пример: /nick Cana117 Sasha_Craft (до 15 ников за раз)")
        res = await asyncio.gather(*[mc_free(n) for n in names])
        return await r("\n".join(f"{n}: {t}" for n, t in zip(names, res)) + "\nСвободен не значит гарантия: имя может быть в резерве. Перед покупкой проверь на namemc.com.")
    if uid == ADMIN and cmd == "/price":
        if len(arg) < 3: return await r("Пример: /price huge ghostly dragon")
        try: rp, ex = await rap_load(), await exists_load()
        except Exception as e: return await r(f"[PS99 ERROR] {e!r}")
        hits = sorted(((k, v) for k, v in rp.items() if arg.lower() in k.lower()), key=lambda x: -x[1])[:15]
        return await r("\n".join(f"{k}: RAP {short(v)}" + (f", exist {ex[k]:,}" if k in ex else "") for k, v in hits) + "\n(RAP в API обновляется раз в 4 часа)" if hits else "ничего не нашёл")
    if uid == ADMIN and cmd == "/acc":
        sub, _, a = arg.partition(" ")
        if sub == "add" and a:
            q("insert or ignore into accs values(?)", a.strip()); t, _ = await inv_report(a.strip()); return await r("[OK] добавлен\n" + t)
        if sub == "del" and a: q("delete from accs where nick=?", a.strip()); return await r("[OK] удалён")
        return await r("\n".join(n for (n,) in q("select nick from accs")) or "аккаунтов нет. /acc add НикRoblox")
    if uid == ADMIN and cmd == "/new":
        await ps99_new(); return await r("[OK] проверил. Если что-то новое есть, пришлю отдельным сообщением")
    if uid == ADMIN and cmd == "/inv":
        sub, _, a = arg.partition(" ")
        if sub == "add" and a:
            mm = re.match(r"^(.+?)\s+(\d+)$", a); n, qty = (mm.group(1), int(mm.group(2))) if mm else (a, 1)
            q("insert into inv values(?,?) on conflict(name) do update set qty=qty+?", n.lower(), qty, qty); return await r(f"[OK] {n} x{qty}")
        if sub == "del" and a: q("delete from inv where name=?", a.lower()); return await r("[OK] удалено")
        accs = [n for (n,) in q("select nick from accs")]
        if accs and not sub: return await r("\n\n".join([(await inv_report(n))[0] for n in accs]))
        rows = q("select name,qty from inv")
        try: d = {k.lower(): v for k, v in (await rap_load()).items()}
        except Exception: d = {}
        tot, lines = 0, []
        for n, qn in rows:
            v = d.get(n, 0) * qn; tot += v; lines.append(f"{n} x{qn}: {v:,}" if v else f"{n} x{qn}: цена не найдена")
        return await r("\n".join(lines) + f"\n\nИтого RAP: {tot:,}" if rows else "пусто. Пример: /inv add huge cat golden 2")
    if cmd == "/mc":
        if uid != ADMIN: return await r("[ACCESS DENIED] только для админа")
        if not arg: return await r("Пример: /mc средневековая башня 9x9 из булыжника с крышей")
        await r("[MC] генерирую..."); info, blob = await mc(uid, arg)
        if blob: await tg("sendDocument", data={"chat_id": uid, "caption": info}, files={"document": ("build.schem", blob)})
        else: await r(info)
        return
    if cmd == "/agent":
        hist_pref[uid] = None if arg in ("", "auto") else arg; return await r(f"[AGENT] {arg or 'auto'}")
    if cmd == "/link":
        c = str(random.randint(100000, 999999)); pending[c] = (uid, time.time() + 600)
        return await r(f"[LINK] Код {c} (10 минут). В веб-терминале введи: /link {c}")
    if cmd == "/migrate_chat":
        s = " | ".join(x["content"][:150] for x in hist_load_tail(sid)) or "пусто"
        q("delete from hist where sid=?", sid); hadd(sid, "user", "Контекст из прошлого чата: " + s); return await r("[MIGRATED] " + s)
    if cmd == "/key":
        sub, _, a = arg.partition(" "); ks = q("select key from keys where owner=?", uid)
        if sub == "add" and a:
            q("insert or ignore into keys values(?,?)", uid, a.strip())
            if m.get("message_id"): await tg("deleteMessage", json={"chat_id": uid, "message_id": m["message_id"]})
            return await r("[OK] ключ добавлен, сообщение удалено")
        if sub == "del" and a.isdigit() and 0 < int(a) <= len(ks): q("delete from keys where owner=? and key=?", uid, ks[int(a) - 1][0]); return await r("[OK] удалён")
        if sub == "sessions": return await r(f"[SESSIONS] привязано веб-устройств: {q('select count(*) from links where uid=?', uid)[0][0]}")
        if sub == "unlink": q("delete from links where uid=?", uid); return await r("[OK] веб-сессии отвязаны")
        return await r("\n".join(f"{i}. {mask(k)}" for i, (k,) in enumerate(ks, 1)) or "ключей нет. /key add sk-or-...")
    if uid == ADMIN and (cmd == "/admin" or cmd[1:] in ADMIN_SUBS):
        sub, a = (arg.split(" ", 1) + [""])[:2] if cmd == "/admin" else (cmd[1:], arg)
        return await r(await admin(sub.lower(), a.strip()))
    c = q("select text from cmds where name=?", cmd.lstrip("/"))
    if c: return await r(c[0][0])
    if cmd.startswith("/"): return await r("Не знаю такой команды. /help")
    await r(await reply(sid, uid, txt, hist_pref.get(uid)))

def hist_load_tail(sid): return hload(sid)[-10:]

async def safe(m):
    try: await handle(m)
    except Exception as e: log.exception("handle: %r", e)

async def poll():
    off = 0
    try:
        me = await tg("getMe")
        if not me.get("ok"): raise RuntimeError(str(me))
        STAT["bot"] = "@" + me["result"]["username"]; await tg("deleteWebhook")
    except Exception as e: STAT["bot"] = "UNREACHABLE"; STAT["err"] = repr(e); log.error("telegram: %r", e)
    while True:
        try:
            r = await tg("getUpdates", json={"offset": off, "timeout": 50})
            if not r.get("ok"): STAT["err"] = str(r); log.error("getUpdates: %s", r); await asyncio.sleep(10); continue
            STAT["bot"] = "OK"
            for up in r.get("result", []):
                off = up["update_id"] + 1
                if "message" in up: asyncio.create_task(safe(up["message"]))
                elif "callback_query" in up:
                    cq = up["callback_query"]; await tg("answerCallbackQuery", json={"callback_query_id": cq["id"]})
                    asyncio.create_task(safe({"from": cq["from"], "text": cq["data"], "message_id": 0}))
        except Exception as e: STAT["err"] = repr(e); log.error("poll: %r", e); await asyncio.sleep(10)

# ---------- Scheduler ----------
last_yt, live_tw = {}, set()
async def job_news(_=None): await say(ADMIN, await ps99())
async def job_mod(_=None): await refresh_models()
async def job_ping(_=None):
    u = E("RENDER_EXTERNAL_URL")
    if u:
        async with httpx.AsyncClient(timeout=20) as c: await c.get(u + "/api/health")
async def job_streams(_=None):
    await ps99_movers(); await ps99_watch()
    async with httpx.AsyncClient(timeout=30) as c:
        for ch in [x for x in E("YT_CHANNELS", "").split(",") if x]:
            try:
                x = (await c.get("https://www.youtube.com/feeds/videos.xml?channel_id=" + ch)).text
                vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", x).group(1); title = re.findall(r"<title>(.*?)</title>", x)[1]
            except Exception: continue
            if ch in last_yt and last_yt[ch] != vid: await say(ADMIN, f"[YOUTUBE] {title}\nhttps://youtu.be/{vid}")
            last_yt[ch] = vid
        cid, sec, us = E("TWITCH_ID"), E("TWITCH_SECRET"), [x for x in E("TWITCH_USERS", "").split(",") if x]
        if cid and sec and us:
            try:
                t = (await c.post("https://id.twitch.tv/oauth2/token", params={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"})).json()["access_token"]
                d = (await c.get("https://api.twitch.tv/helix/streams", params=[("user_login", x) for x in us], headers={"Client-Id": cid, "Authorization": "Bearer " + t})).json()["data"]
            except Exception: return
            live = {s["user_login"]: s["title"] for s in d}
            for n, tt in live.items():
                if n not in live_tw: await say(ADMIN, f"[TWITCH LIVE] {n}: {tt}")
            live_tw.clear(); live_tw.update(live)
KEEP, GAP = 5, 6 * 3600   # хранить 5 копий; новая копия заменяет самую свежую, если ей меньше 6 часов
last_bk = {}
def chat_for(t): return ADMIN if t == BOT else (E("BACKUP_CHAT_ID") or ADMIN)
def who(t): return "чат-бот" if t == BOT else "бот бэкапов"
def bk_target():
    t = (q("select v from settings where k='backup_token'") or [(None,)])[0][0] or E("BACKUP_BOT_TOKEN")
    return (t, chat_for(t)) if t else (BOT, ADMIN)

def valid_db(path):
    try:
        c = sqlite3.connect(path)
        ok = c.execute("pragma integrity_check").fetchone()[0] == "ok" and c.execute("select 1 from sqlite_master where name='users'").fetchone() is not None
        c.close(); return ok
    except Exception: return False

async def send_backup(token, chat, snap, new_gen):
    prev, now = last_bk.get(token, []), int(time.time())
    if prev and not new_gen and now - prev[0][2] < GAP: keep, drop = prev[1:KEEP], [prev[0]] + prev[KEEP:]
    else: keep, drop = prev[:KEEP - 1], prev[KEEP - 1:]
    cap = f"BACKUP v1 {dt.datetime.now(MSK):%d.%m.%Y %H:%M} МСК | " + ",".join(f"{a}:{b}:{c}" for a, b, c in keep)
    with open(snap, "rb") as f:
        r = await tg("sendDocument", token, data={"chat_id": chat, "caption": cap, "disable_notification": "true"}, files={"document": ("backup.db", f)})
    if not r.get("ok"): return r.get("description", "ошибка Telegram")
    mid, fid = r["result"]["message_id"], r["result"]["document"]["file_id"]
    await tg("pinChatMessage", token, json={"chat_id": chat, "message_id": mid, "disable_notification": True})
    for a, _, _ in drop: await tg("deleteMessage", token, json={"chat_id": chat, "message_id": int(a)})
    last_bk[token] = [(mid, fid, now)] + keep

async def job_backup(_=None, force=False):
    global dirty
    if not (dirty or force): return
    if not q("select count(*) from users")[0][0]: return "база пустая, бэкап пропущен (чтобы не затереть хорошие копии)"
    dest = []
    for t in (bk_target()[0], BOT):
        if t and t not in dest: dest.append(t)
    flush_logs(); dirty = False; db.commit(); snap = "/tmp/snap.db"
    dst = sqlite3.connect(snap); db.backup(dst); dst.close(); errs = []
    for t in dest:
        e = await send_backup(t, chat_for(t), snap, force)
        if e: errs.append(f"{who(t)}: {e}")
    if errs: log.error("backup: %s", errs)
    if len(errs) == len(dest): dirty = True
    return "; ".join(errs) or None

async def bk_scan():
    cands, seen = [], set()
    for token in (bk_target()[0], E("BACKUP_BOT_TOKEN"), BOT):
        if not token or token in seen: continue
        seen.add(token)
        try: pm = (await tg("getChat", token, json={"chat_id": chat_for(token)})).get("result", {}).get("pinned_message") or {}
        except Exception: continue
        d = pm.get("document")
        if not d: continue
        cap, old = pm.get("caption", ""), []
        for p in (cap.split("| ", 1)[1] if "| " in cap else "").split(","):
            x = p.split(":")
            if len(x) == 3 and x[0].isdigit() and x[2].isdigit(): old.append((int(x[0]), x[1], int(x[2])))
        chain = [(pm["message_id"], d["file_id"], pm["date"])] + old
        last_bk.setdefault(token, chain[:KEEP])
        for _, fid, ts in chain: cands.append((ts, token, fid, f"{who(token)}, {dt.datetime.fromtimestamp(ts, MSK):%d.%m %H:%M}"))
    return sorted(cands, reverse=True)

async def restore(force=False):
    if not BOT or not ADMIN: return "бот не настроен"
    if not force and q("select count(*) from users")[0][0]: await bk_scan(); return "база на месте"
    cands = await bk_scan()
    if not cands: return "бэкапов не нашёл"
    for _, token, fid, label in cands:
        try:
            fp = (await tg("getFile", token, json={"file_id": fid}))["result"]["file_path"]
            async with httpx.AsyncClient(timeout=60) as c: blob = (await c.get(f"{TGAPI}/file/bot{token}/{fp}")).content
            open("/tmp/restore.db", "wb").write(blob)
            if not valid_db("/tmp/restore.db"): log.error("restore: копия %s битая, беру старее", label); continue
            if force: b = sqlite3.connect("/tmp/before_restore.db"); db.backup(b); b.close()
            src = sqlite3.connect("/tmp/restore.db"); src.backup(db); src.close(); db.executescript(SCHEMA)
            log.info("restore: восстановлено из %s", label); return f"восстановлено из копии ({label})"
        except Exception as e: log.error("restore %s: %r", label, e)
    return "ни одна копия не открылась"

JOBS = {"dg": job_digests, "news": job_news, "yt": job_streams, "bk": job_backup, "mod": job_mod, "ping": job_ping}

async def sched():
    done = set()
    while True:
        n = dt.datetime.now(MSK)
        for tag, ok in (("dg-" + n.strftime("%F-%H:%M"), True), ("news-" + n.strftime("%F-%H"), n.hour in (10, 21)), ("yt-" + n.strftime("%F-%H"), True),
                        ("bk-" + n.strftime("%F-%H-") + str(n.minute // 30), True),
                        ("mod-" + n.strftime("%F-") + str(n.hour // 6), True), ("ping-" + n.strftime("%F-%H-") + str(n.minute // 10), True)):
            if ok and tag not in done:
                done.add(tag); asyncio.create_task(JOBS[tag.split("-")[0]](n.strftime("%H:%M")))
        flush_logs(); asyncio.create_task(job_posts())
        if len(done) > 500: done.clear()
        await asyncio.sleep(30)

# ---------- Agent-Integrator: веб <-> Telegram ----------
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[o for o in E("WEB_ORIGIN", "*").split(",")], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def start():
    rs = await restore(); log.info("start v%s: %s", VERSION, rs)
    if not q("select 1 from sources limit 1"):
        q("insert into sources(owner,kind,url,title) values(0,'rss',?,'Лента.ру')", E("NEWS_RSS", "https://lenta.ru/rss/news"))
    asyncio.create_task(refresh_models()); asyncio.create_task(sched())
    if BOT:
        asyncio.create_task(poll()); asyncio.create_task(say(ADMIN, f"[DEPLOY] v{VERSION}: сервис перезапущен. База: {rs}. Пиши сюда, разберём правки."))

@app.get("/api/health")
def health(): return {"bot": STAT["bot"], "err": STAT["err"], "keys": len(keys_for(0)), "free_models": len(FREE), "pollinations": bool(PKEY), "admin_set": bool(ADMIN)}

@app.get("/")
def index(): return FileResponse("index.html")

def web_sid(cid, chat):
    r = q("select uid from links where cid=?", cid)
    return (f"tg:{r[0][0]}", r[0][0]) if r else (f"web:{cid}:{chat}", 0)

@app.post("/api/chat")
async def chat(req: Request):
    ip, now = req.headers.get("x-forwarded-for", req.client.host).split(",")[0], time.time()
    ipc[ip] = [t for t in ipc.get(ip, []) if t > now - 60]
    if len(ipc[ip]) >= 20: return JSONResponse({"reply": "[RATE LIMIT REACHED]: Лимит исчерпан. Восстановление через 1 минут."}, 429)
    ipc[ip].append(now); b = await req.json()
    text = str(b.get("text", ""))[:6000]; cid = str(b.get("cid", ""))[:40]
    if not text or not cid: return JSONResponse({"reply": "[ERROR] пустой запрос"}, 400)
    sid, uid = web_sid(cid, str(b.get("chat", ""))[:20])
    if uid and q("select banned from users where id=?", uid) == [(1,)]: return JSONResponse({"reply": "[BANNED]"}, 403)
    return {"reply": await reply(sid, uid, text, hist_pref.get(uid))}

@app.post("/api/link")
async def link(req: Request):
    b = await req.json(); p = pending.get(str(b.get("code", "")))
    if not p or p[1] < time.time(): return JSONResponse({"reply": "[LINK ERROR] код неверен или истёк"}, 400)
    q("insert or replace into links values(?,?)", str(b.get("cid", ""))[:40], p[0]); pending.pop(str(b["code"]), None)
    await say(p[0], "[LINK] Веб-терминал привязан: история общая.")
    return {"reply": "[LINK OK] история теперь общая с Telegram"}

@app.post("/api/admin/{sub}")
async def adm(sub: str, req: Request):
    if not ADMIN_TOKEN or not hmac.compare_digest(req.headers.get("x-admin-token", ""), ADMIN_TOKEN): return JSONResponse({"error": "forbidden"}, 403)
    try: a = str((await req.json()).get("arg", ""))
    except Exception: a = ""
    return {"result": await admin(sub if sub in ADMIN_SUBS else "status", a)}
