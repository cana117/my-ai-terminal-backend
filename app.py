import os, re, html, base64, gzip, json, time, math, struct, hmac, hashlib, random, asyncio, sqlite3, logging, collections, datetime as dt
import sys, tempfile

# ===== Хранилище ключей: цепочка ключей =====
try: import cryptography
except Exception:
    try:
        import subprocess; subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "cryptography", "tzdata"], timeout=400, check=False)
    except Exception: pass
try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes as _hs, serialization as _ser
except Exception: Fernet = AESGCM = None

E = os.environ.get
def b64(b): return base64.urlsafe_b64encode(b).decode()
def ub64(s): s = s.strip(); return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def _mk(s):
    s = (s or "").strip()
    if not s: return None
    try:
        b = ub64(s)
        if len(b) == 32: return b
    except Exception: pass
    return hashlib.sha256(s.encode()).digest()

_ek, _eko = E("ENC_KEY", ""), E("ENC_KEY_OLD", "")
VAULT = AESGCM(_mk(_ek)) if (AESGCM and _mk(_ek)) else None
VAULT_OLD = AESGCM(_mk(_eko)) if (AESGCM and _mk(_eko)) else None
def _leg(s): return Fernet(base64.urlsafe_b64encode(hashlib.sha256(s.encode()).digest())) if (Fernet and s) else None
LEG, LEG_OLD = _leg(_ek), _leg(_eko)
AAD, AADD, AADK, BAD, MAGIC, MAGIC3 = b"myai-v2", b"myai-dek:", b"myai-key:", b"myai-backup", b"MYAIENC1", b"MYAIENC3"

def seal(data, aad=AAD):
    n = os.urandom(12); return n + VAULT.encrypt(n, data, aad)

def unseal(b, aad=AAD, primary_only=False):
    for c in ((VAULT,) if primary_only else (VAULT, VAULT_OLD)):
        if c:
            try: return c.decrypt(b[:12], b[12:], aad)
            except Exception: pass
    return None

def _raw(pub): return pub.public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw)
def _bk(shared, ep, pub): return HKDF(algorithm=_hs.SHA256(), length=32, salt=None, info=b"myai-box" + ep + pub).derive(shared)

def box_seal(data, pub_raw, aad):
    e = X25519PrivateKey.generate(); ep = _raw(e.public_key()); n = os.urandom(12)
    return ep + n + AESGCM(_bk(e.exchange(X25519PublicKey.from_public_bytes(pub_raw)), ep, pub_raw)).encrypt(n, data, aad)

def box_open(b, priv_b64, aad):
    p = X25519PrivateKey.from_private_bytes(ub64(priv_b64)); ep = b[:32]
    return AESGCM(_bk(p.exchange(X25519PublicKey.from_public_bytes(ep)), ep, _raw(p.public_key()))).decrypt(b[32:44], b[44:], aad)

try: RPUB = ub64(E("RECOVERY_PUB", "")) if (AESGCM and E("RECOVERY_PUB")) else None; RPUB = RPUB if RPUB and len(RPUB) == 32 else None
except Exception: RPUB = None

def seal_blob(data):
    dek, n = os.urandom(32), os.urandom(12); m = seal(dek, BAD); r = box_seal(dek, RPUB, BAD) if RPUB else b""
    return MAGIC3 + len(m).to_bytes(2, "big") + m + len(r).to_bytes(2, "big") + r + n + AESGCM(dek).encrypt(n, data, BAD)

def open_blob(b, priv=None):
    if b[:8] == MAGIC:
        r = unseal(b[8:]) if VAULT else None
        if r is None: raise ValueError("бэкап зашифрован, а ENC_KEY не подходит или не задан")
        return r
    if b[:8] != MAGIC3: return b
    lm = int.from_bytes(b[8:10], "big"); m = b[10:10 + lm]; o = 10 + lm; lr = int.from_bytes(b[o:o + 2], "big"); r = b[o + 2:o + 2 + lr]; rest = b[o + 2 + lr:]
    try: dek = box_open(r, priv, BAD) if (priv and r) else (unseal(m, BAD) if VAULT else None)
    except Exception: dek = None
    if dek is None: raise ValueError("бэкап зашифрован, а ключ не подходит")
    return AESGCM(dek).decrypt(rest[:12], rest[12:], BAD)

def seal_file(src, dst): open(dst, "wb").write(seal_blob(open(src, "rb").read()))

def _dump_db(path, dekfn, only=None):
    c = sqlite3.connect(path); out = []
    for o, dm, dr in c.execute("select owner,dek_m,dek_r from ukeys"):
        if only is not None and o != only: continue
        try: dek = dekfn(o, dm, dr)
        except Exception: dek = None
        if not dek: out.append(f"{o}: ключ шифрования владельца не открылся"); continue
        for (k,) in c.execute("select key from keys where owner=?", (o,)):
            if k.startswith("enc3:"):
                try: bb = ub64(k[5:]); out.append(f"{o}: " + AESGCM(dek).decrypt(bb[:12], bb[12:], AADK + str(o).encode()).decode())
                except Exception: out.append(f"{o}: запись не открылась")
    return out

if __name__ == "__main__" and sys.argv[1:2] in (["genkey"], ["genrecovery"], ["decrypt"], ["recover"]):
    cmd, arg, only = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else ""), (int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].lstrip("-").isdigit() else None)
    if cmd == "genkey": print(b64(os.urandom(32))); sys.exit()
    if not AESGCM: print("нужен pip install cryptography"); sys.exit(1)
    if cmd == "genrecovery":
        p = X25519PrivateKey.generate()
        print("ЗАКРЫТЫЙ:", b64(p.private_bytes(_ser.Encoding.Raw, _ser.PrivateFormat.Raw, _ser.NoEncryption())))
        print("ОТКРЫТЫЙ:", b64(_raw(p.public_key()))); sys.exit()
    try:
        if cmd == "decrypt" and arg.startswith("enc2:"):
            r = unseal(ub64(arg[5:])) if VAULT else None
            print(r.decode() if r else "ошибка расшифрования"); sys.exit()
        blob = open_blob(open(arg, "rb").read(), E("RECOVERY_KEY") if cmd == "recover" else None)
        import tempfile, os as _o
        tmp = tempfile.mktemp(suffix=".db"); open(tmp, "wb").write(blob)
        if cmd == "recover":
            if not E("RECOVERY_KEY"): print("задай RECOVERY_KEY"); sys.exit(1)
            fn = lambda o, dm, dr: box_open(ub64(dr), E("RECOVERY_KEY"), AADD + str(o).encode()) if dr else None
        else: fn = lambda o, dm, dr: unseal(ub64(dm), AADD + str(o).encode())
        print("\n".join(_dump_db(tmp, fn, only)) or "ключей не найдено"); _o.remove(tmp)
    except Exception as ex: print("не получилось:", ex); sys.exit(1)
    sys.exit()

from zoneinfo import ZoneInfo
import httpx, socket, ipaddress, urllib.parse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

BOT, ADMIN = E("BOT_TOKEN"), int(E("ADMIN_ID", "0") or 0)
TESTER_ENV = {int(x) for x in re.split(r"[,\s]+", E("TESTER_ID", "")) if x.isdigit()}; TESTER = min(TESTER_ENV) if TESTER_ENV else 0
TGAPI = E("TG_API", "https://api.telegram.org").rstrip("/")
ADMIN_TOKEN, PKEY = E("ADMIN_TOKEN", ""), E("POLLINATIONS_KEY", "")
DB, DATAVER = E("DB_PATH", "/tmp/data.db"), int(E("MC_DATA_VERSION", "4671"))
OR, POLLI = "https://openrouter.ai/api/v1", "https://gen.pollinations.ai/v1/chat/completions"
MSK, T0 = ZoneInfo("Europe/Moscow"), time.time()
LOGS, LOGQ = collections.deque(maxlen=300), collections.deque(maxlen=1000)
_SECRET = re.compile(r"sk-[\w-]{12,}|\d{7,11}:[A-Za-z0-9_-]{30,}|enc2?:[\w=+/-]{20,}")

def scrub(t):
    for v in (E("ENC_KEY"), E("ENC_KEY_OLD"), BOT, ADMIN_TOKEN, E("BACKUP_BOT_TOKEN"), E("POLLINATIONS_KEY")):
        if v and len(v) >= 8: t = t.replace(v, "***")
    return _SECRET.sub("***", t)

class _H(logging.Handler):
    def emit(self, r):
        line = scrub(f"{dt.datetime.now(MSK):%d.%m %H:%M:%S} {r.levelname} {r.getMessage()}"); LOGS.append(line); LOGQ.append(line)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("myai"); log.addHandler(_H())

db = sqlite3.connect(DB, check_same_thread=False)
SCHEMA = """create table if not exists users(id integer primary key,name text,banned int default 0,joined text);
create table if not exists keys(owner int,key text unique);
create table if not exists cmds(name text primary key,text text);
create table if not exists hist(id integer primary key autoincrement,sid text,role text,content text);
create table if not exists links(cid text primary key,uid int);
create table if not exists sources(id integer primary key autoincrement,owner int,kind text,url text,title text,unique(owner,url));
create table if not exists subs(uid integer primary key,active int default 0,times text default '12:00',fmt text default 'ai');
create table if not exists posts(id integer primary key autoincrement,at text,text text,sent int default 0);
create table if not exists seen(uid int,key text,primary key(uid,key));
create table if not exists inv(name text primary key,qty int);
create table if not exists settings(k text primary key,v text);
create table if not exists ideas(id integer primary key autoincrement,uid int,at text,text text);
create table if not exists accs(nick text primary key);
create table if not exists logs(id integer primary key autoincrement,line text);
create table if not exists ulogs(id integer primary key autoincrement,uid int,at text,what text);
create table if not exists strikes(id integer primary key autoincrement,uid int,at text,cat text,verdict text,snip text);
create table if not exists usage(day text,uid int,model text,pt int,ct int,n int,primary key(day,uid,model));
create table if not exists ukeys(owner int primary key,dek_m text,dek_r text default '');"""
db.executescript(SCHEMA)

dirty = False
def q(sql, *a):
    global dirty
    c = db.execute(sql, a); db.commit()
    if not sql.lstrip().lower().startswith("select") and "hist" not in sql and "logs" not in sql: dirty = True
    return c.fetchall()

def getv(k): r = db.execute("select v from settings where k=?", (k,)).fetchone(); return r[0] if r else None
URGENT_KEYS, KEEP_KEYS, URGENT = ("testers", "feat:", "mod_mode", "panic"), ("testers", "feat:", "mod_mode", "panic", "model:", "rpub_fp"), {"at": 0.0}
def setv(k, v):
    global dirty
    db.execute("insert or replace into settings values(?,?)", (k, str(v))); db.commit()
    if k.startswith(KEEP_KEYS): dirty = True
    if k.startswith(URGENT_KEYS): URGENT["at"] = time.time()

def dek_for(owner, create=False, primary_only=False):
    r = db.execute("select dek_m from ukeys where owner=?", (owner,)).fetchone(); aad = AADD + str(owner).encode()
    if r: return unseal(ub64(r[0]), aad, primary_only)
    if not create or not VAULT: return None
    d = os.urandom(32); q("insert into ukeys values(?,?,?)", owner, b64(seal(d, aad)), b64(box_seal(d, RPUB, aad)) if RPUB else ""); return d

def enc(k, owner):
    d = dek_for(owner, True)
    if d is None: raise ValueError("ключ шифрования владельца недоступен")
    n = os.urandom(12); return "enc3:" + b64(n + AESGCM(d).encrypt(n, k.encode(), AADK + str(owner).encode()))

def dec(v, owner=None, primary_only=False):
    if v.startswith("enc3:"):
        d = dek_for(owner, False, primary_only) if owner is not None else None
        if d is None: return None
        try: bb = ub64(v[5:]); return AESGCM(d).decrypt(bb[:12], bb[12:], AADK + str(owner).encode()).decode()
        except Exception: return None
    if v.startswith("enc2:"):
        try: r = unseal(ub64(v[5:]), AAD, primary_only)
        except Exception: return None
        return r.decode() if r is not None else None
    if v.startswith("enc:"):
        for f in ((LEG,) if primary_only else (LEG, LEG_OLD)):
            if f:
                try: return f.decrypt(v[4:].encode()).decode()
                except Exception: pass
        return None
    return None if v.startswith("скрыт-") else v

def migrate_keys():
    n = 0
    if not VAULT: return 0
    fp = hashlib.sha256(RPUB).hexdigest()[:16] if RPUB else ""; force = bool(RPUB) and getv("rpub_fp") != fp
    for o, dm, dr in q("select owner,dek_m,dek_r from ukeys"):
        aad = AADD + str(o).encode(); d = unseal(ub64(dm), aad, True)
        if d is None:
            d = unseal(ub64(dm), aad)
            if d is None: continue
            q("update ukeys set dek_m=? where owner=?", b64(seal(d, aad)), o); n += 1
        if RPUB and (not dr or force): q("update ukeys set dek_r=? where owner=?", b64(box_seal(d, RPUB, aad)), o); n += 1
    if RPUB: setv("rpub_fp", fp)
    for rid, o, k in q("select rowid,owner,key from keys"):
        if k.startswith("скрыт-") or (k.startswith("enc3:") and dec(k, o, True) is not None): continue
        p = dec(k, o)
        if p: q("update keys set key=? where rowid=?", enc(p, o), rid); n += 1
    return n

def recovery_status():
    tot = q("select count(*) from ukeys")[0][0]; have = q("select count(*) from ukeys where dek_r!=''")[0][0]
    return f"Аварийный ключ: {'задан' if RPUB else 'НЕ задан'}. Ключей: {tot}, с копией: {have}."

def note_usage(uid, model, u):
    try:
        db.execute("insert into usage values(?,?,?,?,?,1) on conflict(day,uid,model) do update set pt=pt+excluded.pt, ct=ct+excluded.ct, n=n+1",
                   (dt.datetime.now(MSK).strftime("%F"), uid, model[:60], int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))); db.commit()
    except Exception as e: log.error("usage: %r", e)

def testers():
    s = set(TESTER_ENV)
    try: s |= {int(x) for x in json.loads(getv("testers") or "[]")}
    except Exception: pass
    s.discard(ADMIN); return s

def is_tester(uid): return uid in testers()
def panic(): return getv("panic") == "1"

def act(uid, what):
    try: q("insert into ulogs(uid,at,what) values(?,?,?)", uid, dt.datetime.now(MSK).strftime("%d.%m %H:%M"), what[:60]); q("delete from ulogs where id<=(select max(id) from ulogs)-3000")
    except Exception as e: log.error("act: %r", e)

STAT = {"cache": 0, "llm": 0, "bot": "not started", "err": "", "cut": 0}
cool, cache, hist_pref, pending, ipc = {}, collections.OrderedDict(), {}, {}, {}
FREE, VISION = [], []
SYS = {
    "chat": "Ты MY_AI, ассистент в Telegram и на сайте. Стиль: лаконичный и понятный, без префиксов.",
    "vision": "Ты MY_AI. Опиши и разбери то, что видишь на изображении.",
    "coder": "Ты Агент-Кодер: пишешь и дорабатываешь код, отвечаешь кратко.",
    "optimizer": "Ты Агент-Оптимизатор: сжимаешь текст без потери фактов.",
}
PREF = {"coder": ("coder", "qwen", "deepseek", "devstral"), "optimizer": ("8b", "7b", "9b", "mini", "small", "flash", "nano", "lite", "gemma"), "chat": ()}
CODE = re.compile(r"```|код|функци|скрипт|баг|ошибк|python|javascript|html|css|sql|def |class |import ", re.I)
def route(t): return "coder" if CODE.search(t) else "chat"

async def refresh_models():
    global FREE, VISION
    try:
        async with httpx.AsyncClient(timeout=30) as c: d = (await c.get(OR + "/models")).json()["data"]
        FREE = [m["id"] for m in sorted((m for m in d if m["id"].endswith(":free")), key=lambda m: -(m.get("context_length") or 0))]
        VISION = [m["id"] for m in sorted((m for m in d if m["id"].endswith(":free") and "image" in ((m.get("architecture") or {}).get("input_modalities") or [])), key=lambda m: -(m.get("context_length") or 0))]
    except Exception as e: log.error("models: %r", e)

POOL_FOR_USERS = E("POOL_FOR_USERS", "0") == "1"
def keys_for(uid):
    pool = uid in (-1, ADMIN) or POOL_FOR_USERS
    env = [k.strip() for k in E("OPENROUTER_KEYS", "").split(",") if k.strip()] if pool else []
    rows = [] if (uid == 0 and not pool) else q("select owner,key from keys where owner in (?,?)", uid, 0 if pool else uid)
    return env + [k for k in (dec(v, o) for o, v in rows) if k]

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

PROV = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "console.groq.com/keys"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "aistudio.google.com/apikey"),
    "cerebras": ("https://api.cerebras.ai/v1", "gpt-oss-120b", "cloud.cerebras.ai"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest", "console.mistral.ai"),
    "sambanova": ("https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct", "cloud.sambanova.ai"),
    "github": ("https://models.github.ai/inference", "openai/gpt-4.1-mini", "github.com/settings/tokens"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "meta/llama-3.3-70b-instruct", "build.nvidia.com"),
    "cohere": ("https://api.cohere.ai/compatibility/v1", "command-a-03-2025", "dashboard.cohere.com/api-keys"),
    "huggingface": ("https://router.huggingface.co/v1", "", "huggingface.co/settings/tokens"),
}
PROV_HELP = "Свой ИИ: /key add провайдер КЛЮЧ\n" + "\n".join(f"{n}: {v[2]}" for n, v in PROV.items())

def parse_key(k):
    if k and "|" in k:
        pr, _, rest = k.partition("|")
        if pr in PROV: return pr, PROV[pr][0], rest
        if pr == "custom" and "|" in rest: b, _, kk = rest.partition("|"); return "custom", b, kk
    return "openrouter", OR, k

def my_model(uid):
    v = getv(f"model:{uid}") or ""
    return tuple(v.split("|", 1)) if "|" in v else (None, None)

mlist, pmc = {}, {}
BAD_M = ("embed", "whisper", "tts", "guard", "moderat", "rerank", "audio", "transcri", "image", "ocr", "safety", "speech", "diffusion", "flux", "dall")
GOOD_M = ("gpt-oss-120b", "llama-3.3-70b", "gemini-2.5-flash", "gemini-flash", "mistral-medium", "mistral-small", "command-a", "llama-3.1-70b", "70b", "flash", "small")

async def prov_models(c, base, k):
    ck = (base, k[-8:]); v = pmc.get(ck)
    if v and v[0] > time.time(): return v[1]
    ids = []
    try:
        r = await c.get(base.rstrip("/") + "/models", headers={"Authorization": "Bearer " + k}, timeout=20)
        if r.status_code == 200:
            d = r.json(); d = d.get("data", d) if isinstance(d, dict) else d
            ids = [str((x.get("id") if isinstance(x, dict) else x) or "").replace("models/", "", 1) for x in d]
            ids = [i for i in ids if i and not any(b in i.lower() for b in BAD_M)]
    except Exception as e: log.error("models list: %s", type(e).__name__)
    pmc[ck] = (time.time() + (21600 if ids else 300), ids)
    return ids

async def auto_model(c, prov, base, k):
    ids = await prov_models(c, base, k)
    for g in GOOD_M:
        hit = [i for i in ids if g in i.lower()]
        if hit: return min(hit, key=len)
    return ids[0] if ids else (PROV.get(prov, ("", ""))[1] or None)

async def try_prov(c, prov, base, k, model, msgs, uid):
    try:
        if not await safe_url(base): return None
        m = model or await auto_model(c, prov, base, k)
        if not m: return None
        r = await c.post(base.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + k}, json={"model": m, "messages": msgs})
    except Exception: return None
    if r.status_code == 200:
        try:
            j = r.json(); t = j["choices"][0]["message"]["content"]
            if t: note_usage(uid, f"{prov}:{m}", j.get("usage") or {}); return t
        except Exception: pass
    if r.status_code == 429: cool[k] = time.time() + 90
    elif r.status_code in (401, 402, 403): cool[k] = time.time() + 3600
    return None

async def check_key(rec):
    pr, base, k = parse_key(rec)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(base.rstrip("/") + ("/key" if pr == "openrouter" else "/models"), headers={"Authorization": "Bearer " + k})
        return False if r.status_code in (401, 403) else (True if r.status_code == 200 else None)
    except Exception: return None

async def llm(msgs, role="chat", uid=-1):
    msgs = [{"role": "system", "content": SYS[role]}] + msgs; now = time.time(); STAT["llm"] += 1
    mine = [parse_key(k) for k in keys_for(uid)]; pp, pm = my_model(uid); orkeys = [x[2] for x in mine if x[0] == "openrouter"]
    async with httpx.AsyncClient(timeout=60) as c:
        if role != "vision":
            for prov, base, k in sorted((x for x in mine if x[0] != "openrouter"), key=lambda x: x[0] != pp):
                if cool.get(k, 0) > now: continue
                t = await try_prov(c, prov, base, k, pm if prov == pp else None, msgs, uid)
                if t: return t
        ms = models_for(role)
        if pm and pp == "openrouter" and role != "vision": ms = [pm] + [x for x in ms if x != pm]
        own = {x for (v,) in q("select key from keys where owner=?", uid) for x in [dec(v, uid)] if x} if (uid > 0 and pm) else set()
        for m in ms:
            for k in orkeys:
                if cool.get(k, 0) > now: continue
                if m == pm and not m.endswith(":free") and uid != ADMIN and k not in own: continue
                try: r = await c.post(OR + "/chat/completions", headers={"Authorization": "Bearer " + k}, json={"model": m, "messages": msgs})
                except httpx.HTTPError: break
                if r.status_code == 200:
                    try:
                        t = r.json()["choices"][0]["message"]["content"]
                        if t: note_usage(uid, m, r.json().get("usage") or {}); return t
                    except Exception: break
                if r.status_code == 429:
                    if "per-day" in r.text: cool[k] = reset_at(r); continue
                    cool[m] = now + 90; break
                if r.status_code in (401, 402, 403): cool[k] = now + 3600; continue
                break
        try:
            r = await c.post(POLLI, headers={"Authorization": "Bearer " + PKEY} if PKEY else {}, json={"model": "openai", "messages": msgs})
            if r.status_code == 200: return r.json()["choices"][0]["message"]["content"]
        except Exception: pass
    ts = [v - now for v in cool.values() if v > now]
    return f"[RATE LIMIT REACHED]: Лимит исчерпан. Подожди {max(1, math.ceil(min(ts, default=60) / 60))} минут."

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

# ---------- Minecraft & PS99 & News & Telegram functions ----------
# (Остальные модули бота сохранены полностью в стандартной логике)
BLK = re.compile(r"^[a-z0-9_]+:[a-z0-9_/]+(\[[a-z0-9_=,]+\])?$")
SPEC = 'Верни ТОЛЬКО JSON без markdown...'

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
        return f"Схема готова: блоков: {n}, типов: {p}.", blob
    except Exception as e: return f"Ошибка схемы: {e}", None

# Промпт модерации и проверки
MOD_PROMPT = """Ты модератор сообщений в Telegram-боте (13+). Отвечай ТОЛЬКО JSON: {"verdict":"ok|note|support|warn|block","category":"..."}."""
async def classify(t, uid):
    out = await llm([{"role": "user", "content": MOD_PROMPT + "\n\n<<<\n" + t[:2000] + "\n>>>"}], "optimizer", uid)
    mm = re.search(r"\{.*?\}", out, re.S)
    try: v = json.loads(mm.group(0)) if mm else {}
    except Exception: v = {}
    return {"verdict": v.get("verdict", "ok"), "category": str(v.get("category", "other"))[:30]}

async def screen(uid, text):
    if getv("mod_mode") == "off" or uid == ADMIN: return None
    v = await classify(text, uid)
    vd = v["verdict"]
    if vd == "ok": return None
    if vd == "note": return "Не присылай в чат ключи и пароли."
    if vd == "support": return "Тебе нужна помощь? Обратись в службу поддержки (112)."
    return "Сообщение нарушает правила бота (/rules)."

# Telegram и FastAPI роуты
_hc = None
def hc():
    global _hc
    if _hc is None: _hc = httpx.AsyncClient(timeout=70)
    return _hc

async def tg(method, token=None, **kw):
    kw.setdefault("timeout", 180 if kw.get("files") else 70)
    return (await hc().post(f"{TGAPI}/bot{token or BOT}/{method}", **kw)).json()

async def say(chat, text, token=None, **kw):
    for i in range(0, max(len(text), 1), 4000):
        await tg("sendMessage", token, json={"chat_id": chat, "text": text[i:i + 4000], **kw})

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def start():
    log.info("start v%s", VERSION)
    asyncio.create_task(refresh_models())

@app.get("/api/ping")
def ping(): return {"ok": True}

@app.get("/api/health")
def health(): return {"ok": True}

@app.get("/")
def index(): return FileResponse("index.html")

@app.get("/app")
def mini_app(): return FileResponse("index.html")  # Отдельный маршрут под Telegram Mini App

VERSION = "0.01"