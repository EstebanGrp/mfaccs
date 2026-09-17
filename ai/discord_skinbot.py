#!/usr/bin/env python3
# ============================================================
# MiniFeather SkinBot — bot de Discord que administra la DB de
# skins compartidas (accounts.json en EstebanGrp/mfaccs) vía la API de GitHub.
#
# Comandos (slash) en un canal autorizado:
#   /skin set    <jugador> <skin>     → asigna skin (id custom:mf_*,
#                                       vanilla, ruta /skins/)
#   /skin seturl <jugador> <url>      → asigna skin por URL de PNG
#   /skin remove <jugador>            → quita el override
#   /skin list   [página]             → lista la DB
#   /skin reload                      → re-descarta cambios locales
#   /skin sync                        → fuerza PUSH del archivo actual
#
# Flujo:
#   1. Un admin escribe el comando en Discord.
#   2. El bot edita accounts.json con la API de contents de GitHub
#      (GET → SHA → PUT con mensaje de commit).
#   3. TODOS los clientes de la extensión ven el cambio al poco
#      tiempo: CustomSkins.js hace fetch del accounts.json remoto
#      (raw.githubusercontent.com) y lo fusiona con el empaquetado.
#
# Requisitos:
#   pip install discord.py requests
#   Variables de entorno (o edita las constantes abajo):
#     MFSB_TOKEN       → token del bot (Discord Developer Portal)
#     MFSB_GH_TOKEN    → token de GitHub con permiso contents:write
#     MFSB_REPO        → owner/repo  (default EstebanGrp/mfaccs)
#     MFSB_BRANCH      → rama (default main)
#     MFSB_ADMINS      → ids de Discord separados por coma
#     MFSB_CHANNEL     → id del canal autorizado (opcional)
#
# Arranque:
#   python ai/discord_skinbot.py
# ============================================================
import base64
import hashlib
import json
import random
import os
import re
import secrets
import sys
import time
from collections import deque

try:
    import discord
    from discord import app_commands
    from discord.ext import commands
except ImportError:
    print("[SkinBot] Falta discord.py  →  pip install discord.py")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[SkinBot] Falta requests  →  pip install requests")
    sys.exit(1)

# ── configuración ──
TOKEN = os.environ.get("MFSB_TOKEN", "")
GH_TOKEN = os.environ.get("MFSB_GH_TOKEN", "")
REPO = os.environ.get("MFSB_REPO", "EstebanGrp/mfaccs")
BRANCH = os.environ.get("MFSB_BRANCH", "main")
ACCOUNTS_PATH = "accounts.json"
ADMINS = [s.strip() for s in os.environ.get("MFSB_ADMINS", "").split(",") if s.strip()]
CHANNEL_ID = os.environ.get("MFSB_CHANNEL", "")
LOG_CHANNEL_ID = os.environ.get("MFSB_LOG_CHANNEL", "1549572492434346015")

# DB local de cuentas MiniFeather (NO va al repo público: contraseñas)
#   ai/mf_accounts.json  → { "cuentas": { "<usuario>": {...} } }
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mf_accounts.json")

GH_API = f"https://api.github.com/repos/{REPO}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{ACCOUNTS_PATH}"
SKINS_DIR = "skins"  # repo público: mfaccs/skins/<user>/<ts>.png
SKINS_RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SKINS_DIR}"
# Push de updates en vivo: el client escucha este topic via SSE y recarga
# la DB al instante (sin esperar su polling de respaldo).
SKINS_PUSH_TOPIC = os.environ.get("MFSB_PUSH_TOPIC", "mf-skins-updates-v1")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SKIN_RE = re.compile(r"^[a-z0-9_]+$", re.I)


def warn(*a):
    print("[SkinBot]", *a, file=sys.stderr)


CONGRATS = [
    "Every journey begins before you know where it will lead.",
    "A new name carries no history, only possibility.",
    "You cannot change the beginning, but you can shape what follows.",
    "What you build today becomes the world you wake up in tomorrow.",
    "Some paths are discovered only after you take the first step.",
    "A blank page is not empty. It is waiting.",
    "The world remembers what you choose to leave behind.",
    "You start with nothing, but nothing is where everything begins.",
    "Every choice closes a door and opens a path.",
    "Time turns moments into memories, and memories into stories.",
    "You don't need a past to give meaning to a beginning.",
    "The first step means nothing until you decide where to take the second.",
    "Even the smallest block can become part of something greater.",
    "A beginning has no meaning until someone gives it one.",
    "Perhaps the point was never to reach the end, but to see what you became along the way.",
]

async def notify_new_account(username, creator: discord.abc.User):
    try:
        ch = bot.get_channel(int(LOG_CHANNEL_ID))
        if ch is None:
            ch = await bot.fetch_channel(int(LOG_CHANNEL_ID))
        msg = random.choice(CONGRATS)
        await ch.send(
            f"🆕 New MiniFeather account: **{username}** — created by {creator.mention}\n{msg}"
        )
    except Exception as e:
        warn("notify_new_account falló:", repr(e))


def gh_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ── GitHub: leer / escribir accounts.json ──
def gh_download():
    """Descarga accounts.json del repo. Devuelve (data, sha|None)."""
    r = requests.get(
        f"{GH_API}/contents/{ACCOUNTS_PATH}?ref={BRANCH}",
        headers=gh_headers(),
        timeout=15,
    )
    if r.status_code == 404:
        return {"players": {}}, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    sha = j["sha"]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = _salvage_accounts_json(content)
        if data is None:
            warn("accounts.json remoto corrupto: no se pudo reparar")
            return None, sha
        warn("accounts.json remoto corrupto — auto-reparado (wrapper players)")
        return data, sha
    if isinstance(data, dict) and "players" not in data and data and all(
        isinstance(v, dict) for v in data.values()
    ):
        # dict de players al ras (sin wrapper): envolver
        warn("accounts.json remoto sin wrapper 'players' — auto-reparado")
        return {"players": data}, sha
    return data, sha


_TOP_PAIR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*(\{)', re.DOTALL)


def _salvage_accounts_json(content: str):
    """Recupera accounts.json dañado por escrituras viejas del panel-set.
    Caso real observado: falta la clave 'players' (el dict de jugadores
    quedó al ras) y a veces sobra/falta una llave de cierre. Estrategia:
    extraer cada '"clave": { ... }' de nivel superior y reconstruir
    {"players": {...}}. Devuelve None si no se recupera nada."""
    out = {}
    pos = 0
    for m in _TOP_PAIR_RE.finditer(content):
        key = m.group(1)
        b = m.start(2)
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(b, len(content)):
            c = content[j]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            # valor incompleto: tomar hasta el final y cerrar llaves
            frag = content[b:].strip().rstrip(',') + '}' * max(1, depth)
            try:
                out[key] = json.loads(frag)
            except json.JSONDecodeError:
                pass
            break
        try:
            out[key] = json.loads(content[b:end + 1])
        except json.JSONDecodeError:
            pass
    if not out:
        return None
    if set(out.keys()) == {"players"}:
        # ya venía con su wrapper: no doble-envolver
        return out
    return {"players": out}


def gh_upload(data, sha, msg):
    """Sube accounts.json. Devuelve la URL del commit."""
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    payload = {
        "message": msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"{GH_API}/contents/{ACCOUNTS_PATH}",
        headers=gh_headers(),
        json=payload,
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")
    commit_sha = r.json().get("commit", {}).get("sha", "")
    push_skins_update(commit_sha, msg)
    return r.json().get("commit", {}).get("html_url", "")


def push_skins_update(commit_sha, msg):
    """Avisa por ntfy que la DB cambió — los clients conectados recargan al
    instante via SSE. Fire-and-forget: si ntfy falla, el polling de respaldo
    del client (cada 60s) cubre."""
    try:
        requests.post(
            f"https://ntfy.sh/{SKINS_PUSH_TOPIC}",
            data=f"{commit_sha} {msg}".encode("utf-8")[:512],
            headers={"Title": "mfaccs update", "Priority": "default",
                     "Tags": "art"},
            timeout=8,
        )
    except Exception as e:
        warn("ntfy push falló:", repr(e))


def skin_slug(user):
    """Nombre de carpeta limpio para skins/<slug>/: usernames con '_'
    final (ej. shusukegxe_) generan rutas feas — se recortan los '_' de
    los extremos."""
    s = user.strip().strip("_")
    return s if re.match(r"^[A-Za-z0-9_-]{2,64}$", s) else user


def gh_upload_png(user, png_bytes):
    """Sube skins/<user>/<ts>.png al repo publico (carpeta por usuario,
    versiones por timestamp — nunca se pisan). Devuelve la URL raw."""
    slug = skin_slug(user)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = f"{SKINS_DIR}/{slug}/{ts}.png"
    payload = {
        "message": f"skin upload: {user}",
        "content": base64.b64encode(png_bytes).decode("ascii"),
        "branch": BRANCH,
    }
    r = requests.put(f"{GH_API}/contents/{path}", headers=gh_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")
    return f"{SKINS_RAW}/{slug}/{ts}.png"


EXTERNAL_RE = re.compile(r"^https?://(?!raw\.githubusercontent\.com)", re.I)


def rehost_external_skin(user, url):
    """Descarga un PNG de una URL externa (minecraftskins.com, etc.) y lo
    re-sube al repo propio. Así el client lo carga desde raw.githubusercontent
    (que SÍ permite CORS) en vez de chocar con el hotlink-block del origen.
    Devuelve la URL raw, o None si la descarga falló."""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (skinbot)"})
        if r.status_code != 200:
            warn(f"rehost: HTTP {r.status_code} para {url[:80]}")
            return None
        png = r.content
        if not png.startswith(b"\x89PNG"):
            warn(f"rehost: no es PNG ({len(png)} bytes) para {url[:80]}")
            return None
        if len(png) > 16 * 1024 * 1024:
            warn(f"rehost: PNG demasiado grande ({len(png)} bytes)")
            return None
        return gh_upload_png(user, png)
    except Exception as e:
        warn("rehost falló:", repr(e))
        return None


def validate_skin_value(value):
    """Mismas reglas que normalizeSkinValue de CustomSkins.js."""
    v = (value or "").strip()
    if not v:
        return False, "vacío"
    if v.startswith("custom:"):
        return True, "id custom del client"
    if re.match(r"^(https?://|chrome-extension://|file://|data:image/|blob:)", v, re.I):
        return True, "URL absoluta"
    if "/" in v:
        return True, "ruta /skins/"
    if SKIN_RE.match(v):
        return True, "id vanilla"
    return False, "formato no reconocido"


# ── cuentas MiniFeather (local, con contraseñas hasheadas) ──
USER_RE = re.compile(r"^[a-z0-9_]{3,16}$", re.I)

# En GitHub Actions el disco es efímero: el archivo de cuentas vive en un
# REPO PRIVADO y se sincroniza por la API de contents (igual que skins).
#   MFSB_ACC_REPO  → owner/repo privado (ej. EstebanGrp/mfaccs-private)
#   MFSB_ACC_PATH  → nombre del archivo (default mf_accounts.json)
# Sin esas vars, sigue usando el archivo local como siempre.
ACC_REPO = os.environ.get("MFSB_ACC_REPO", "")
ACC_PATH = os.environ.get("MFSB_ACC_PATH", "mf_accounts.json")
ACC_API = f"https://api.github.com/repos/{ACC_REPO}/contents/{ACC_PATH}" if ACC_REPO else ""
REMOTE_ACC = bool(ACC_REPO)


def _acc_gh_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


_acc_sha_cache = {"sha": None}


def acc_download():
    """Trae las cuentas del repo privado. Devuelve data o None si falla."""
    r = requests.get(f"{ACC_API}?ref={BRANCH}", headers=_acc_gh_headers(), timeout=15)
    if r.status_code == 404:
        return {"cuentas": {}}
    r.raise_for_status()
    j = r.json()
    _acc_sha_cache["sha"] = j.get("sha")
    content = base64.b64decode(j["content"]).decode("utf-8")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        warn("mf_accounts remoto corrupto:", e)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cuentas"), dict):
        return {"cuentas": {}}
    return data


def acc_upload(data):
    """Sube las cuentas al repo privado (PUT con sha, retry en 409)."""
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    payload = {
        "message": "skinbot: accounts sync",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if _acc_sha_cache["sha"]:
        payload["sha"] = _acc_sha_cache["sha"]
    r = requests.put(ACC_API, headers=_acc_gh_headers(), json=payload, timeout=20)
    if r.status_code == 409:
        # sha vencido: re-descargamos y reintentamos una vez
        acc_download()
        payload["sha"] = _acc_sha_cache["sha"]
        r = requests.put(ACC_API, headers=_acc_gh_headers(), json=payload, timeout=20)
    if r.status_code not in (200, 201):
        warn(f"acc_upload GitHub {r.status_code}: {r.text[:200]}")
        return False
    _acc_sha_cache["sha"] = r.json().get("content", {}).get("sha")
    return True


def load_local_accounts():
    """Lee las cuentas. En modo remoto (Actions) viene del repo privado;
    localmente de ai/mf_accounts.json. Estructura:
    { "cuentas": { "<usuario>": {
          "hash": "<pbkdf2$iter$salt$dk>",   # contraseña
          "discord_id": "123...",             # cuenta de Discord vinculada
          "discord_tag": "nombre",            # nombre en Discord al vincular
          "created": 1690000000,
          "creator": "<discord_id>",
          "note": "..." } } }
    """
    if REMOTE_ACC:
        try:
            data = acc_download()
            if data is not None:
                return data
        except Exception as e:
            warn("acc_download falló, sigo con lo local:", e)
    if not os.path.exists(ACCOUNTS_FILE):
        return {"cuentas": {}}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("cuentas"), dict):
            return {"cuentas": {}}
        return data
    except Exception as e:
        warn("mf_accounts.json corrupto:", e)
        return {"cuentas": {}}


def save_local_accounts(data):
    if REMOTE_ACC:
        if not acc_upload(data):
            raise RuntimeError("no se pudo sincronizar el repo de cuentas")
        return
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    f = None
    os.replace(tmp, ACCOUNTS_FILE)


def hash_password(password: str):
    """PBKDF2-HMAC-SHA256, 200k iteraciones, formato portable."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2$200000${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_b64, dk_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iters))
        return secrets.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


# ── bot de Discord ──
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


def actor_id(i: discord.Interaction) -> str:
    return str(i.user.id)


def is_admin(i) -> bool:
    return not ADMINS or actor_id(i) in ADMINS


def in_channel(i) -> bool:
    if not CHANNEL_ID:
        return True
    return str(i.channel_id) == CHANNEL_ID


@bot.event
async def on_ready():
    # registrar la vista persistente: sin esto, los botones de paneles
    # enviados ANTES de un reinicio (como el de Actions cada ~6h) mueren
    # con "The application did not respond"
    bot.add_view(PanelView())
    await tree.sync()
    warn(f"SkinBot listo como {bot.user} — repo {REPO}@{BRANCH}")
    warn(f"canal autorizado: {CHANNEL_ID or '(todos)'} | admins: {len(ADMINS) or '(todos)'}")
    # listener de creaciones de cuenta desde el client (ntfy)
    bot.loop.create_task(ntfy_client_accounts_loop())


# ── creaciones de cuenta desde el client (ntfy) ────────────────────
NTFY_ACC_TOPIC = os.environ.get("MFSB_NTFY_ACC_TOPIC", "mf-accounts-req-v1")
NTFY_SEEN_MAX = 500
_ntfy_seen: deque = deque(maxlen=NTFY_SEEN_MAX)


def is_valid_client_request(req):
    """Valida la petición de creación que envía el client por ntfy."""
    if not isinstance(req, dict) or req.get("type") != "mf_account_create":
        return False, "formato"
    username = str(req.get("username") or "")
    if not USER_RE.match(username):
        return False, "username"
    if not isinstance(req.get("password"), str) or len(req["password"]) < 6 or len(req["password"]) > 64:
        return False, "password"
    if not isinstance(req.get("at"), (int, float)) or abs(time.time() - req["at"]) > 600:
        return False, "timestamp"
    return True, username


async def handle_client_account_create(req):
    """Crea la cuenta pedida desde el client y anuncia en el canal de log."""
    ok, why = is_valid_client_request(req)
    username = str(req.get("username") or "")
    password = str(req.get("password") or "")
    skin = str(req.get("skin") or "").strip() or None
    if not ok:
        warn(f"client create rechazada ({why}):", str(req)[:200])
        return
    data = load_local_accounts()
    cuentas = data["cuentas"]
    key = username.lower()
    if key in cuentas:
        warn(f"client create: {key} ya existe")
        return
    cuentas[key] = {
        "hash": hash_password(password),
        "discord_id": "",
        "discord_tag": f"client:{req.get('client', '?')}",
        "created": int(time.time()),
        "creator": "client",
        "source": "client",
    }
    try:
        save_local_accounts(data)
    except Exception as e:
        warn("client create: no se pudo guardar:", repr(e))
        return
    # skin inicial opcional → accounts.json público
    if skin:
        try:
            ok_skin, _why = validate_skin_value(skin)
            if ok_skin:
                sdata, sha = gh_download()
                if sdata is not None:
                    players = sdata.setdefault("players", {})
                    entry = {k: v for k, v in players.get(key, {}).items() if k != "skin"}
                    entry["skin"] = skin
                    players[key] = entry
                    gh_upload(sdata, sha, f"skinbot: client create {key}")
        except Exception as e:
            warn("client create: skin inicial falló:", repr(e))
    try:
        ch = bot.get_channel(int(LOG_CHANNEL_ID)) or await bot.fetch_channel(int(LOG_CHANNEL_ID))
        await ch.send(f"🆕 Account **{username}** created from the MiniFeather Client")
    except Exception as e:
        warn("notify client create falló:", repr(e))
    warn(f"client create OK: {key}")


async def ntfy_client_accounts_loop():
    """Suscripción WS al topic ntfy de peticiones del client.
    El client manda el JSON plano (con la contraseña real dentro; ntfy
    es público así que esto es SOLO para el ecosistema de confianza —
    la contraseña llega hasheada a la DB, nunca en claro)."""
    import asyncio
    ws = None
    backoff = 2
    while not bot.is_closed():
        try:
            import asyncio
            import websockets
            uri = f"wss://ntfy.sh/{NTFY_ACC_TOPIC}/ws?since=30s"
            async with websockets.connect(uri) as w:
                ws = w
                backoff = 2
                async for raw in w:
                    try:
                        packet = json.loads(raw)
                        if packet.get("event") != "message":
                            continue
                        mid = packet.get("id") or packet.get("time")
                        if mid in _ntfy_seen:
                            continue
                        _ntfy_seen.append(mid)
                        try:
                            req = json.loads(packet.get("message") or "")
                        except json.JSONDecodeError:
                            continue
                        await handle_client_account_create(req)
                    except Exception as e:
                        warn("ntfy msg error:", repr(e))
        except Exception as e:
            warn(f"ntfy ws error: {e!r} — reintento en {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


@tree.command(name="skin", description="Administrar la DB de skins compartidas (accounts.json)")
@app_commands.describe(
    action="set / seturl / remove / list / reload / sync",
    player="username o uuid del jugador",
    skin="id de skin (custom:mf_..., vanilla, ruta /skins/)",
    url="URL del PNG (para seturl)",
    page="página para list",
)
@app_commands.choices(action=[
    app_commands.Choice(name="set", value="set"),
    app_commands.Choice(name="seturl", value="seturl"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="list", value="list"),
    app_commands.Choice(name="reload", value="reload"),
    app_commands.Choice(name="sync", value="sync"),
])
async def skin_cmd(interaction: discord.Interaction,
                   action: str, player: str = "", skin: str = "",
                   url: str = "", page: int = 1):
    if not in_channel(interaction):
        await interaction.response.send_message("Canal no autorizado.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return
    # defer: las llamadas a GitHub pueden tardar
    await interaction.response.defer(ephemeral=True)

    act = (action or "").lower()

    try:
        # ── acciones sin descargar ──
        if act == "sync":
            data, sha = gh_download()
            if data is None:
                await interaction.followup.send("accounts.json remoto corrupto.")
                return
            commit = gh_upload(data, sha, "skinbot: sync")
            await interaction.followup.send(f"Re-subido tal cual.\n{commit}")
            return

        if act == "reload":
            data, _sha = gh_download()
            if data is None:
                await interaction.followup.send("accounts.json remoto corrupto.")
                return
            n = len(data.get("players", {}))
            await interaction.followup.send(f"Descartado. Remote tiene {n} entradas.")
            return

        # ── acciones con jugador ──
        if act in ("set", "seturl", "remove"):
            key = (player or "").strip()
            if not key:
                await interaction.followup.send("Falta <player>.")
                return
            is_uuid = bool(UUID_RE.match(key))
            kdisp = "uuid" if is_uuid else "username"
            key = key.lower()

            data, sha = gh_download()
            if data is None:
                await interaction.followup.send("accounts.json remoto corrupto.")
                return
            players = data.setdefault("players", {})

            if act == "remove":
                if key not in players:
                    await interaction.followup.send(f"No hay override para `{key}`.")
                    return
                del players[key]
                commit = gh_upload(data, sha, f"skinbot: remove {key}")
                await interaction.followup.send(f"Quitado ({kdisp}).\n{commit}")
                return

            value = (skin if act == "set" else url).strip()
            if act == "seturl":
                if not re.match(r"^https?://", value, re.I):
                    await interaction.followup.send("seturl exige http(s)://…")
                    return
            else:
                ok, why = validate_skin_value(value)
                if not ok:
                    await interaction.followup.send(f"Skin inválida ({why}).")
                    return
            # re-host de URLs externas (CORS desde miniblox.io)
            rehosted = False
            if EXTERNAL_RE.match(value):
                raw = rehost_external_skin(key, value)
                if raw:
                    value, rehosted = raw, True
                else:
                    await interaction.followup.send(
                        "No pude descargar esa imagen (¿es un link directo a PNG?). "
                        "Sube el archivo con /skinupload.")
                    return
            old = players.get(key, {}).get("skin")
            # preservar campos extra (rank, name) al reescribir la entrada
            entry = {k: v for k, v in players.get(key, {}).items() if k != "skin"}
            entry["skin"] = value
            players[key] = entry
            commit = gh_upload(data, sha, f"skinbot: set {key} = {value[:40]}")
            oldinfo = f" (antes: `{old}`)" if old and old != value else ""
            rh = "\nRe-hosteada en nuestro repo (sin CORS)." if rehosted else ""
            await interaction.followup.send(
                f"OK — `{key}` ({kdisp}) → `{value}`{oldinfo}{rh}\n{commit}")

        elif act == "list":
            data, _sha = gh_download()
            if data is None:
                await interaction.followup.send("accounts.json remoto corrupto.")
                return
            entries = sorted(data.get("players", {}).items())
            PER = 15
            total = len(entries)
            pages = max(1, (total + PER - 1) // PER)
            page = max(1, min(page, pages))
            chunk = entries[(page - 1) * PER: page * PER]
            lines = [f"**DB de skins** — {total} entradas (pág {page}/{pages}):"]
            for k, v in chunk:
                sv = (v or {}).get("skin", "?")
                if len(sv) > 42:
                    sv = sv[:39] + "…"
                lines.append(f"`{k}` → {sv}")
            await interaction.followup.send("\n".join(lines))

        else:
            await interaction.followup.send(
                "Acción desconocida. Usa set / seturl / remove / list / reload / sync.")

    except Exception as e:
        warn("comando falló:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


@tree.command(name="skinupload", description="Upload your own skin PNG — it becomes your in-game skin")
@app_commands.describe(image="Square or 2:1 PNG skin file (64-2048px, power of 2)")
async def skinupload_cmd(interaction: discord.Interaction, image: discord.Attachment):
    if not in_channel(interaction):
        await interaction.response.send_message("Unauthorized channel.", ephemeral=True)
        return
    # público: cualquiera del canal puede subir la SUYA (requiere cuenta vinculada)
    await interaction.response.defer(ephemeral=True)

    try:
        # 1. debe tener cuenta MiniFeather vinculada
        did = str(interaction.user.id)
        recs = load_local_accounts().get("cuentas", {})
        mine = [u for u, r in recs.items() if r.get("discord_id") == did]
        if not mine:
            await interaction.followup.send(
                "You need a MiniFeather account first — use the panel's **Create account** button."
            )
            return
        user = mine[0]

        # 2. validar el archivo
        if (image.content_type or "").lower() not in ("image/png",):
            await interaction.followup.send("File must be a PNG.")
            return
        if image.size > 16 * 1024 * 1024:
            await interaction.followup.send("Max 16 MB.")
            return
        png = await image.read()

        # 3. subir a mfaccs/skins/<user>.png y setear la URL como skin
        raw_url = gh_upload_png(user, png)
        data, sha = gh_download()
        if data is None:
            await interaction.followup.send("Remote accounts.json corrupt.")
            return
        players = data.setdefault("players", {})
        # preservar campos extra (rank, name) al reescribir la entrada
        entry = {k: v for k, v in players.get(user, {}).items() if k != "skin"}
        entry["skin"] = raw_url
        players[user] = entry
        commit = gh_upload(data, sha, f"skinbot: skinupload {user}")
        await interaction.followup.send(
            f"Skin uploaded — `{user}` → `{raw_url}`\nVisible in-game in ≤5 min.\n{commit}"
        )

    except Exception as e:
        warn("skinupload falló:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


# ── /skins myownskins — galería de tus PNGs en mfaccs/skins/<user>/ ──
def gh_list_user_skins(user):
    """Lista todas las skins del usuario en skins/<slug>/*.png.
    Devuelve [(nombre, url_raw, fecha)] ordenadas por fecha (nueva primero)."""
    slug = skin_slug(user)
    r = requests.get(f"{GH_API}/contents/{SKINS_DIR}/{slug}?ref={BRANCH}",
                     headers=gh_headers(), timeout=15)
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:200]}")
    out = []
    for it in r.json():
        if it.get("type") != "file" or not it.get("name", "").lower().endswith(".png"):
            continue
        out.append((it["name"], it.get("download_url") or f"{SKINS_RAW}/{slug}/{it['name']}",
                    it.get("commit", {}).get("date") or it.get("last_commit", {}).get("date", "")))
    out.sort(key=lambda t: t[2], reverse=True)
    return out


class OwnSkinsView(discord.ui.View):
    """Embed paginado con preview y botón para activar la skin mostrada."""
    def __init__(self, user, skins):
        super().__init__(timeout=300)
        self.user = user
        self.skins = skins
        self.page = 0

    def embed(self):
        name, url, date = self.skins[self.page]
        total = len(self.skins)
        d = date[:10].replace("-", "/") if date else "?"
        emb = discord.Embed(
            title=f"🎨 Skins de {self.user}",
            description=f"**{name}**\n"
                        f"`{url}`\n"
                        f"Subida: {d} · {self.page + 1}/{total}",
            color=0x8B5CF6)
        emb.set_image(url=url)
        emb.set_footer(text="Use the ⚡ button to make the shown skin your active one")
        return emb

    def sync_buttons(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == len(self.skins) - 1

    async def update(self, interaction: discord.Interaction):
        self.sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self.update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < len(self.skins) - 1:
            self.page += 1
        await self.update(interaction)

    @discord.ui.button(label="⚡ Activate", style=discord.ButtonStyle.green)
    async def activate(self, interaction: discord.Interaction, button: discord.ui.Button):
        _name, url, _d = self.skins[self.page]
        try:
            data, sha = gh_download()
            if data is None:
                await interaction.response.send_message("Remote accounts.json corrupt.", ephemeral=True)
                return
            players = data.setdefault("players", {})
            entry = {k: v for k, v in players.get(self.user, {}).items() if k != "skin"}
            entry["skin"] = url
            players[self.user] = entry
            commit = gh_upload(data, sha, f"skinbot: activate {self.user} from gallery")
            emb = self.embed()
            emb.set_footer(text=f"✔ Active — {commit or 'applied'}")
            await interaction.response.edit_message(embed=emb, view=self)
        except Exception as e:
            warn("activate falló:", repr(e))
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


@tree.command(name="skins", description="Ver tus skins subidas al repo (galería con preview)")
@app_commands.describe(what="myownskins — tus PNGs en mfaccs/skins/<tu-user>/")
@app_commands.choices(what=[app_commands.Choice(name="myownskins", value="myownskins")])
async def skins_cmd(interaction: discord.Interaction, what: str = "myownskins"):
    if not in_channel(interaction):
        await interaction.response.send_message("Unauthorized channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    try:
        # cuenta vinculada → username → carpeta skins/<slug>/
        did = str(interaction.user.id)
        recs = load_local_accounts().get("cuentas", {})
        mine = [u for u, r in recs.items() if r.get("discord_id") == did]
        if not mine:
            await interaction.followup.send(
                "You need a MiniFeather account first — use the panel's **Create account** button.")
            return
        user = mine[0]

        skins = gh_list_user_skins(user)
        if not skins:
            await interaction.followup.send(
                f"No skins found for `{user}` in `skins/{skin_slug(user)}/`.\n"
                "Upload one with `/skinupload` or from the client panel.")
            return

        view = OwnSkinsView(user, skins)
        view.sync_buttons()
        await interaction.followup.send(embed=view.embed(), view=view)

    except Exception as e:
        warn("skins falló:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


# ── /rank — gestionar rangos custom definidos en accounts.json ──
@tree.command(name="rank", description="Administrar rangos custom (defs + asignación por jugador)")
@app_commands.describe(
    action="setdef=definir/actualizar un rango, list=ver todos, set=asignar a jugador, remove=quitar",
    key="Nombre clave del rango (ej: dev, vip, mvp)",
    label="Etiqueta a mostrar (ej: DEV, VIP)",
    color="Color hex (#00FFFF)",
    glow="Efecto glow (default true)",
    shiny="Efecto shiny (default true)",
    bold="Negrita (default true)",
    priority_base="Rango vanilla del que hereda prioridad (default eternus)",
    player="Username o uuid del jugador (para set/remove)",
)
@app_commands.choices(action=[
    app_commands.Choice(name="setdef", value="setdef"),
    app_commands.Choice(name="list", value="list"),
    app_commands.Choice(name="set", value="set"),
    app_commands.Choice(name="remove", value="remove"),
])
async def rank_cmd(interaction: discord.Interaction, action: str = "list",
                   key: str = None, label: str = None, color: str = None,
                   glow: bool = None, shiny: bool = None, bold: bool = None,
                   priority_base: str = None, player: str = None):
    if not in_channel(interaction):
        await interaction.response.send_message("Unauthorized channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    try:
        if action == "list":
            data, _sha = gh_download()
            if data is None:
                await interaction.followup.send("Remote accounts.json corrupt.")
                return
            ranks = data.get("ranks", {})
            if not ranks:
                await interaction.followup.send("No ranks defined. Use `/rank setdef`.")
                return
            lines = []
            for k, d in sorted(ranks.items()):
                eff = ", ".join(
                    f"{n}={'on' if d.get(n, True) else 'off'}" for n in ("bold", "glow", "shiny"))
                lines.append(f"**{k}** → [{d.get('label', k).upper()}] `{d.get('color', '?')}` "
                             f"({eff}, base={d.get('priorityBase', 'eternus')})")
            asign = [f"`{u}` ({d['rank']})" for u, d in data.get("players", {}).items() if d.get("rank")]
            await interaction.followup.send(
                "**Rank defs:**\n" + "\n".join(lines) +
                ("\n\n**Asignados:**\n" + ", ".join(asign) if asign else ""))
            return

        if action == "setdef":
            if not key:
                await interaction.followup.send("Falta <key> (nombre del rango).")
                return
            if color and not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
                await interaction.followup.send("Color debe ser hex (#00FFFF).")
                return
            data, sha = gh_download()
            if data is None:
                await interaction.followup.send("Remote accounts.json corrupt.")
                return
            ranks = data.setdefault("ranks", {})
            old = ranks.get(key.lower(), {})
            d = {
                "label": (label or old.get("label") or key).upper(),
                "color": color or old.get("color") or "#00FFFF",
                "bold": bold if bold is not None else old.get("bold", True),
                "glow": glow if glow is not None else old.get("glow", True),
                "shiny": shiny if shiny is not None else old.get("shiny", True),
                "priorityBase": priority_base or old.get("priorityBase", "eternus"),
            }
            ranks[key.lower()] = d
            commit = gh_upload(data, sha, f"skinbot: rank setdef {key.lower()}")
            await interaction.followup.send(
                f"Rank **{key.lower()}** definido: `[{d['label']}]` color {d['color']}, "
                f"glow={'on' if d['glow'] else 'off'}, shiny={'on' if d['shiny'] else 'off'}.\n"
                f"Aplica en vivo en ≤5 min.\n{commit}")
            return

        # set / remove sobre un jugador
        if not player:
            await interaction.followup.send("Falta <player> (username o uuid).")
            return
        data, sha = gh_download()
        if data is None:
            await interaction.followup.send("Remote accounts.json corrupt.")
            return
        players = data.setdefault("players", {})
        pk = player.strip()
        if action == "set":
            if not key:
                await interaction.followup.send("Falta <key> (rango a asignar).")
                return
            if key.lower() not in data.get("ranks", {}):
                await interaction.followup.send(f"El rango `{key}` no existe — créalo con `/rank setdef`.")
                return
            entry = dict(players.get(pk, {}))
            entry["rank"] = key.lower()
            players[pk] = entry
            commit = gh_upload(data, sha, f"skinbot: rank {pk} = {key.lower()}")
            await interaction.followup.send(
                f"Rank `{key.lower()}` asignado a **{pk}** — visible en vivo en ≤5 min.\n{commit}")
        else:  # remove
            entry = dict(players.get(pk, {}))
            if not entry.get("rank"):
                await interaction.followup.send(f"`{pk}` no tiene rango.")
                return
            del entry["rank"]
            players[pk] = entry
            commit = gh_upload(data, sha, f"skinbot: rank remove {pk}")
            await interaction.followup.send(f"Rank quitado a **{pk}**.\n{commit}")

    except Exception as e:
        warn("rank falló:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


@tree.command(name="mfaccount", description="Cuentas MiniFeather con contraseña, vinculadas a Discord")
@app_commands.describe(
    action="create / link / unlink / passwd / info / list / delete",
    username="nombre de la cuenta MiniFeather",
    password="contraseña (para create/passwd)",
    account="nombre de la cuenta (para link del admin)",
)
@app_commands.choices(action=[
    app_commands.Choice(name="create", value="create"),
    app_commands.Choice(name="link", value="link"),
    app_commands.Choice(name="unlink", value="unlink"),
    app_commands.Choice(name="passwd", value="passwd"),
    app_commands.Choice(name="info", value="info"),
    app_commands.Choice(name="list", value="list"),
    app_commands.Choice(name="delete", value="delete"),
])
async def mfaccount_cmd(interaction: discord.Interaction,
                        action: str, username: str = "",
                        password: str = "", account: str = ""):
    if not in_channel(interaction):
        await interaction.response.send_message("Canal no autorizado.", ephemeral=True)
        return

    act = (action or "").lower()
    uid = str(interaction.user.id)
    data = load_local_accounts()
    cuentas = data["cuentas"]

    def find_mine():
        for name, c in cuentas.items():
            if c.get("discord_id") == uid:
                return name, c
        return None, None

    # ── CREATE: cualquiera puede crear su cuenta ──
    if act == "create":
        if not username or not password:
            await interaction.response.send_message(
                "Uso: /mfaccount create username:pepito password:•••", ephemeral=True)
            return
        if not USER_RE.match(username):
            await interaction.response.send_message(
                "El usuario debe ser 3-16 chars [a-z0-9_].", ephemeral=True)
            return
        if len(password) < 6:
            await interaction.response.send_message(
                "Contraseña mínima: 6 caracteres.", ephemeral=True)
            return
        key = username.lower()
        if key in cuentas:
            await interaction.response.send_message(
                f"`{username}` ya existe.", ephemeral=True)
            return
        name, _ = find_mine()
        if name:
            await interaction.response.send_message(
                f"Ya tienes la cuenta `{name}` vinculada. Usa `/mfaccount unlink` primero.", ephemeral=True)
            return
        cuentas[key] = {
            "hash": hash_password(password),
            "discord_id": uid,
            "discord_tag": str(interaction.user),
            "created": int(time.time()),
            "creator": uid,
        }
        save_local_accounts(data)
        await notify_new_account(username, interaction.user)
        await interaction.response.send_message(
            f"Cuenta `{username}` creada y vinculada a {interaction.user.mention}. "
            "La contraseña vive hasheada (PBKDF2) solo en la PC del bot.", ephemeral=True)
        return

    # ── acciones que requieren dueño o admin ──
    if not is_admin(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    try:
        if act == "link":
            # admin vincula una cuenta existente a un usuario de Discord
            key = (username or "").strip().lower()      # cuenta MiniFeather
            target = (account or "").strip()            # id de Discord
            if not re.match(r"^\d{5,25}$", target):
                await interaction.followup.send(
                    "Uso: /mfaccount link username:<cuenta> account:<discord_id>")
                return
            if key not in cuentas:
                await interaction.followup.send(f"No existe la cuenta `{key}`.")
                return
            old = cuentas[key].get("discord_id")
            cuentas[key]["discord_id"] = target
            save_local_accounts(data)
            oldinfo = f" (antes <@{old}>)" if old and old != target else ""
            await interaction.followup.send(
                f"`{key}` vinculada a <@{target}>{oldinfo}.")

        elif act == "unlink":
            key = (username or "").strip().lower()
            if key not in cuentas:
                await interaction.followup.send(f"No existe la cuenta `{key}`.")
                return
            cuentas[key].pop("discord_id", None)
            cuentas[key].pop("discord_tag", None)
            save_local_accounts(data)
            await interaction.followup.send(f"`{key}` desvinculada de Discord.")

        elif act == "passwd":
            key = (username or "").strip().lower()
            if key not in cuentas:
                await interaction.followup.send(f"No existe la cuenta `{key}`.")
                return
            if len(password) < 6:
                await interaction.followup.send("Contraseña mínima: 6 caracteres.")
                return
            cuentas[key]["hash"] = hash_password(password)
            save_local_accounts(data)
            await interaction.followup.send(f"Contraseña de `{key}` actualizada.")

        elif act == "info":
            key = (username or "").strip().lower()
            if not key:
                name, c = find_mine()
                if not name:
                    await interaction.followup.send("No tienes cuenta vinculada.")
                    return
                key, entry = name, c
            elif key in cuentas:
                entry = cuentas[key]
            else:
                await interaction.followup.send(f"No existe la cuenta `{key}`.")
                return
            did = entry.get("discord_id")
            lines = [f"**{key}**",
                     f"Discord: {f'<@{did}>' if did else '—'}",
                     f"Creada: <t:{entry.get('created', 0)}:R>",
                     f"Creador: <@{entry.get('creator', '0')}>"]
            await interaction.followup.send("\n".join(lines))

        elif act == "list":
            if not cuentas:
                await interaction.followup.send("Sin cuentas aún.")
                return
            lines = ["**Cuentas MiniFeather** — %d:" % len(cuentas)]
            for name, c in sorted(cuentas.items()):
                did = c.get("discord_id")
                who = f"<@{did}>" if did else "sin vincular"
                lines.append(f"`{name}` → {who}")
            await interaction.followup.send("\n".join(lines))

        elif act == "delete":
            key = (username or "").strip().lower()
            if key not in cuentas:
                await interaction.followup.send(f"No existe la cuenta `{key}`.")
                return
            del cuentas[key]
            save_local_accounts(data)
            await interaction.followup.send(f"Cuenta `{key}` eliminada.")

        else:
            await interaction.followup.send(
                "Acción desconocida. create / link / unlink / passwd / info / list / delete")

    except Exception as e:
        warn("mfaccount falló:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


# ── /panel: GUI with buttons + modals ───────────────────────────

# usuarios que pulsaron "Upload skin" esperando su PNG (uid → timestamp)
_PENDING_UPLOADS: dict = {}
PENDING_UPLOAD_TTL = 120  # 2 min


async def _process_png_upload(message):
    """Sube el PNG adjunto como skin del autor (flujo panel Upload skin)."""
    user_id = message.author.id
    ts = _PENDING_UPLOADS.get(user_id)
    if not ts or int(time.time()) - ts > PENDING_UPLOAD_TTL:
        return False
    att = next((a for a in message.attachments
                if (a.content_type or "").lower() == "image/png"), None)
    if not att:
        await message.reply(
            "That's not a PNG — attach a `.png` skin file (square or 2:1, 64–2048px).",
            mention_author=False)
        return True
    _PENDING_UPLOADS.pop(user_id, None)
    recs = load_local_accounts().get("cuentas", {})
    mine = [u for u, r in recs.items() if r.get("discord_id") == str(user_id)]
    if not mine:
        await message.reply("No MiniFeather account found — press **Create account** first.",
                            mention_author=False)
        return True
    user = mine[0]
    if att.size > 16 * 1024 * 1024:
        await message.reply("Max 16 MB.", mention_author=False)
        return True
    try:
        png = await att.read()
        raw_url = gh_upload_png(user, png)
        data, sha = gh_download()
        if data is None:
            await message.reply("Remote accounts.json corrupt.", mention_author=False)
            return True
        players = data.setdefault("players", {})
        entry = {k: v for k, v in players.get(user, {}).items() if k != "skin"}
        entry["skin"] = raw_url
        players[user] = entry
        gh_upload(data, sha, f"skinbot: panel upload {user}")
        await message.reply(
            f"Skin uploaded — `{user}` → `{raw_url}`\nVisible in-game in seconds.",
            mention_author=False)
    except Exception as e:
        warn("panel upload falló:", repr(e))
        try:
            await message.reply(f"Error: {e}", mention_author=False)
        except Exception:
            pass
    return True


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.attachments and str(message.channel.id) == (CHANNEL_ID or str(message.channel.id)):
        try:
            if await _process_png_upload(message):
                return
        except Exception as e:
            warn("on_message upload falló:", repr(e))
    await bot.process_commands(message)


class CreateAccountModal(discord.ui.Modal, title="Create MiniFeather account"):
    username = discord.ui.TextInput(
        label="Miniblox username", placeholder="Your exact in-game username",
        min_length=3, max_length=16)
    password = discord.ui.TextInput(
        label="Password", placeholder="minimum 6 characters",
        min_length=6, max_length=64)

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        username = str(self.username.value).strip()
        password = str(self.password.value)
        data = load_local_accounts()
        cuentas = data["cuentas"]

        if not USER_RE.match(username):
            await interaction.response.send_message(
                "Username must be 3-16 chars [a-z0-9_].", ephemeral=True)
            return
        key = username.lower()
        if key in cuentas:
            await interaction.response.send_message(
                f"`{username}` already exists.", ephemeral=True)
            return
        for name, c in cuentas.items():
            if c.get("discord_id") == uid:
                await interaction.response.send_message(
                    f"You already have the account `{name}` linked.", ephemeral=True)
                return
        cuentas[key] = {
            "hash": hash_password(password),
            "discord_id": uid,
            "discord_tag": str(interaction.user),
            "created": int(time.time()),
            "creator": uid,
        }
        try:
            save_local_accounts(data)
        except Exception as e:
            await interaction.response.send_message(
                f"Could not save (accounts repo?): {e}", ephemeral=True)
            return
        await notify_new_account(username, interaction.user)
        await interaction.response.send_message(
            f"Account **{username}** created and linked to {interaction.user.mention} ✅",
            ephemeral=True)

    async def on_error(self, interaction, error):
        warn("modal create falló:", repr(error))
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            pass


class SetSkinModal(discord.ui.Modal, title="Choose skin for your account"):
    skin = discord.ui.TextInput(
        label="Skin", placeholder="custom:mf_... | chris | devs/itzesteban | https://...",
        min_length=1, max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.skin.value).strip()
        ok, why = validate_skin_value(value)
        if not ok:
            await interaction.response.send_message(
                f"Invalid skin ({why}).", ephemeral=True)
            return
        # the skin is assigned to the user's MiniFeather account
        uid = str(interaction.user.id)
        data = load_local_accounts()
        cuentas = data["cuentas"]
        key = None
        for name, c in cuentas.items():
            if c.get("discord_id") == uid:
                key = name
                break
        if not key:
            await interaction.response.send_message(
                "You don't have a MiniFeather account. Create one with the button above first.",
                ephemeral=True)
            return
        try:
            # URLs externas (minecraftskins.com, etc.) bloquean CORS desde
            # miniblox.io → re-host en nuestro repo antes de guardar
            rehosted = False
            if EXTERNAL_RE.match(value):
                await interaction.response.defer(ephemeral=True)
                raw = rehost_external_skin(key, value)
                if raw:
                    value, rehosted = raw, True
                else:
                    await interaction.followup.send(
                        "Couldn't download that image (is it a direct PNG link?). "
                        "Try uploading the file with `/skinupload` instead.",
                        ephemeral=True)
                    return
            sdata, sha = gh_download()
            if sdata is None:
                await interaction.followup.send(
                    "Remote accounts.json is corrupt.", ephemeral=True)
                return
            players = sdata.setdefault("players", {})
            old = players.get(key, {}).get("skin")
            # preservar campos extra (rank, name) al reescribir la entrada
            entry = {k: v for k, v in players.get(key, {}).items() if k != "skin"}
            entry["skin"] = value
            players[key] = entry
            commit = gh_upload(sdata, sha, f"skinbot: panel set {key} = {value[:40]}")
            oldinfo = f" (before: `{old}`)" if old and old != value else ""
            rh = "\nRe-hosted in our repo (CORS-safe)." if rehosted else ""
            send = interaction.followup.send if rehosted else interaction.response.send_message
            await send(
                f"Skin of **{key}** → `{value}`{oldinfo}{rh}\nVisible in-game within 5 min.{f'  {commit}' if commit else ''}",
                ephemeral=True)
        except Exception as e:
            warn("panel skin falló:", repr(e))
            try:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            except Exception:
                pass

    async def on_error(self, interaction, error):
        warn("modal skin falló:", repr(error))
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            pass


class RankSetDefModal(discord.ui.Modal, title="Define / update a rank"):
    key = discord.ui.TextInput(
        label="Rank key", placeholder="dev / vip / mvp ...",
        min_length=2, max_length=16)
    label = discord.ui.TextInput(
        label="Label shown in-game", placeholder="DEV / VIP / MVP",
        min_length=1, max_length=16)
    color = discord.ui.TextInput(
        label="Color (hex)", placeholder="#00FFFF", default="#00FFFF",
        min_length=4, max_length=9)
    effects = discord.ui.TextInput(
        label="Effects", placeholder="glow:yes shiny:yes bold:yes base:eternus",
        default="glow:yes shiny:yes bold:yes base:eternus",
        min_length=1, max_length=100, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        key = str(self.key.value).strip().lower()
        label = str(self.label.value).strip()
        color = str(self.color.value).strip()
        fx = str(self.effects.value or "").strip()
        if not re.match(r"^[a-z0-9_-]{2,16}$", key):
            await interaction.response.send_message("Key must be [a-z0-9_-] 2-16.", ephemeral=True)
            return
        if not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
            await interaction.response.send_message("Color must be hex (#00FFFF).", ephemeral=True)
            return
        d = {"label": label.upper(), "color": color,
             "bold": True, "glow": True, "shiny": True, "priorityBase": "eternus"}
        for tok in fx.split():
            if ":" not in tok:
                continue
            n, v = tok.split(":", 1)
            n = n.lower()
            if n in ("bold", "glow", "shiny"):
                d[n] = v.strip().lower() in ("yes", "true", "on", "1")
            elif n == "base":
                d["priorityBase"] = v.strip() or "eternus"
        data, sha = gh_download()
        if data is None:
            await interaction.response.send_message("Remote accounts.json corrupt.", ephemeral=True)
            return
        data.setdefault("ranks", {})[key] = d
        commit = gh_upload(data, sha, f"skinbot: panel rank setdef {key}")
        await interaction.response.send_message(
            f"Rank **{key}** → `[{d['label']}]` {d['color']} · "
            f"glow={'on' if d['glow'] else 'off'} shiny={'on' if d['shiny'] else 'off'} "
            f"bold={'on' if d['bold'] else 'off'}\nApplies in-game live.\n{commit}",
            ephemeral=True)

    async def on_error(self, interaction, error):
        warn("modal rank setdef falló:", repr(error))
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            pass


class RankPlayerModal(discord.ui.Modal, title="Assign / remove a rank"):
    player = discord.ui.TextInput(
        label="Player (username or uuid)", placeholder="shusukegxe_ / 6eb7369a-...",
        min_length=2, max_length=40)
    key = discord.ui.TextInput(
        label="Rank key (empty = remove)", placeholder="dev / vip — leave empty to remove",
        max_length=16, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        pk = str(self.player.value).strip()
        rk = str(self.key.value or "").strip().lower()
        data, sha = gh_download()
        if data is None:
            await interaction.response.send_message("Remote accounts.json corrupt.", ephemeral=True)
            return
        players = data.setdefault("players", {})
        entry = dict(players.get(pk, {}))
        if not rk:
            if not entry.get("rank"):
                await interaction.response.send_message(
                    f"`{pk}` has no rank.", ephemeral=True)
                return
            del entry["rank"]
            players[pk] = entry
            commit = gh_upload(data, sha, f"skinbot: panel rank remove {pk}")
            await interaction.response.send_message(
                f"Rank removed from **{pk}**.\n{commit}", ephemeral=True)
            return
        if rk not in data.get("ranks", {}):
            await interaction.response.send_message(
                f"Rank `{rk}` doesn't exist — define it first with **Rank defs**.", ephemeral=True)
            return
        entry["rank"] = rk
        players[pk] = entry
        commit = gh_upload(data, sha, f"skinbot: panel rank {pk} = {rk}")
        await interaction.response.send_message(
            f"Rank `{rk}` assigned to **{pk}** — applies live in-game.\n{commit}",
            ephemeral=True)

    async def on_error(self, interaction, error):
        warn("modal rank player falló:", repr(error))
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            pass


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(label="Create account", style=discord.ButtonStyle.green,
                       emoji="🪶", custom_id="mfsb:crear")
    async def crear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        await interaction.response.send_modal(CreateAccountModal())

    @discord.ui.button(label="Set skin", style=discord.ButtonStyle.blurple,
                       emoji="🎨", custom_id="mfsb:skin")
    async def skin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        await interaction.response.send_modal(SetSkinModal())

    @discord.ui.button(label="My skins", style=discord.ButtonStyle.blurple,
                       emoji="🖼️", custom_id="mfsb:gallery")
    async def gallery(self, interaction: discord.Interaction, button: discord.ui.Button):
        # misma lógica que /skins myownskins
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            did = str(interaction.user.id)
            recs = load_local_accounts().get("cuentas", {})
            mine = [u for u, r in recs.items() if r.get("discord_id") == did]
            if not mine:
                await interaction.followup.send(
                    "You need a MiniFeather account first — press **Create account**.")
                return
            user = mine[0]
            skins = gh_list_user_skins(user)
            if not skins:
                await interaction.followup.send(
                    f"No skins found for `{user}` in `skins/{skin_slug(user)}/`.\n"
                    "Upload one with `/skinupload` or from the client panel.")
                return
            view = OwnSkinsView(user, skins)
            view.sync_buttons()
            await interaction.followup.send(embed=view.embed(), view=view)
        except Exception as e:
            warn("panel gallery falló:", repr(e))
            try:
                await interaction.followup.send(f"Error: {e}")
            except Exception:
                pass

    @discord.ui.button(label="Upload skin", style=discord.ButtonStyle.green,
                       emoji="📤", custom_id="mfsb:upload")
    async def upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        did = str(interaction.user.id)
        recs = load_local_accounts().get("cuentas", {})
        mine = [u for u, r in recs.items() if r.get("discord_id") == did]
        if not mine:
            await interaction.response.send_message(
                "You need a MiniFeather account first — press **Create account**.",
                ephemeral=True)
            return
        # mensaje visible con hint: el usuario responde subiendo el PNG
        await interaction.response.send_message(
            f"{interaction.user.mention} send your skin PNG here (**Reply** to this "
            "message or just attach it in this channel within 2 min) — "
            "square or 2:1 PNG, 64–2048px. It will become your in-game skin.",
            ephemeral=False)
        _PENDING_UPLOADS[interaction.user.id] = int(time.time())

    @discord.ui.button(label="My account", style=discord.ButtonStyle.gray,
                       emoji="ℹ️", custom_id="mfsb:info")
    async def info(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        uid = str(interaction.user.id)
        data = load_local_accounts()
        key, entry = None, None
        for name, c in data["cuentas"].items():
            if c.get("discord_id") == uid:
                key, entry = name, c
                break
        if not key:
            await interaction.response.send_message(
                "You don't have an account yet. Press **Create account** first.", ephemeral=True)
            return
        did = entry.get("discord_id")
        await interaction.response.send_message(
            f"**{key}**\n"
            f"Discord: {f'<@{did}>' if did else '—'}\n"
            f"Created: <t:{entry.get('created', 0)}:R>\n"
            f"Creator: <@{entry.get('creator', '0')}>",
            ephemeral=True)

    @discord.ui.button(label="Rank defs", style=discord.ButtonStyle.gray,
                       emoji="🏷️", custom_id="mfsb:rankdef", row=1)
    async def rankdef(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(RankSetDefModal())

    @discord.ui.button(label="Assign rank", style=discord.ButtonStyle.gray,
                       emoji="🎖️", custom_id="mfsb:rankset", row=1)
    async def rankset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(RankPlayerModal())

    @discord.ui.button(label="Ranks list", style=discord.ButtonStyle.gray,
                       emoji="📋", custom_id="mfsb:ranklist", row=1)
    async def ranklist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            data, _sha = gh_download()
            if data is None:
                await interaction.followup.send("Remote accounts.json corrupt.")
                return
            ranks = data.get("ranks", {})
            if not ranks:
                await interaction.followup.send("No ranks defined — use **Rank defs**.")
                return
            lines = []
            for k, d in sorted(ranks.items()):
                eff = ", ".join(
                    f"{n}={'on' if d.get(n, True) else 'off'}" for n in ("bold", "glow", "shiny"))
                lines.append(f"**{k}** → `[{d.get('label', k).upper()}]` `{d.get('color', '?')}` "
                             f"({eff}, base={d.get('priorityBase', 'eternus')})")
            asign = [f"`{u}` ({d['rank']})" for u, d in data.get("players", {}).items() if d.get("rank")]
            await interaction.followup.send(
                "**Rank defs:**\n" + "\n".join(lines) +
                ("\n\n**Assigned to:**\n" + ", ".join(asign) if asign else ""))
        except Exception as e:
            warn("panel ranklist falló:", repr(e))
            try:
                await interaction.followup.send(f"Error: {e}")
            except Exception:
                pass


PANEL_EMBED = discord.Embed(
    title="🪶 MiniFeather — Accounts, Skins & Ranks",
    description=(
        "Create your MiniFeather account (password-protected, linked to your Discord) "
        "and choose the skin others will see in-game.\n\n"
        "**Upload your own skin:** press **Upload skin** and send your PNG in this "
        "channel (square or 2:1, 64–2048px), or use `/skinupload`. Manage them in **My skins**.\n\n"
        "**Skin formats (Set skin):**\n"
        "`custom:mf_...` — client custom id\n"
        "`chris`, `bob` — Miniblox vanilla\n"
        "`devs/itzesteban` — pack path\n"
        "`https://...png` — absolute URL\n\n"
        "**Ranks (admin):** define custom in-game tags with color and effects "
        "(glow / shiny / bold) and assign them to players — they apply live "
        "without reloading the game."
    ),
    color=0x5865F2,
)
PANEL_EMBED.set_footer(text="Skin & rank changes reach the game in seconds")


@tree.command(name="panel", description="Panel de cuentas y skins de MiniFeather")
async def panel_cmd(interaction: discord.Interaction):
    if not in_channel(interaction):
        await interaction.response.send_message("Canal no autorizado.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("No autorizado.", ephemeral=True)
        return
    await interaction.response.send_message(embed=PANEL_EMBED, view=PanelView())


def main():
    if not TOKEN:
        print("[SkinBot] Falta MFSB_TOKEN (token del bot de Discord).")
        sys.exit(1)
    if not GH_TOKEN:
        print("[SkinBot] Falta MFSB_GH_TOKEN (token de GitHub con contents:write).")
        sys.exit(1)
    warn(f"repo: {REPO}@{BRANCH} · path: {ACCOUNTS_PATH}")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
