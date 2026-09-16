#!/usr/bin/env python3
# ============================================================
# MiniFeather SkinBot â€” bot de Discord que administra la DB de
# skins compartidas (accounts.json en EstebanGrp/mfaccs) vÃ­a la API de GitHub.
#
# Comandos (slash) en un canal autorizado:
#   /skin set    <jugador> <skin>     â†’ asigna skin (id custom:mf_*,
#                                       vanilla, ruta /skins/)
#   /skin seturl <jugador> <url>      â†’ asigna skin por URL de PNG
#   /skin remove <jugador>            â†’ quita el override
#   /skin list   [pÃ¡gina]             â†’ lista la DB
#   /skin reload                      â†’ re-descarta cambios locales
#   /skin sync                        â†’ fuerza PUSH del archivo actual
#
# Flujo:
#   1. Un admin escribe el comando en Discord.
#   2. El bot edita accounts.json con la API de contents de GitHub
#      (GET â†’ SHA â†’ PUT con mensaje de commit).
#   3. TODOS los clientes de la extensiÃ³n ven el cambio al poco
#      tiempo: CustomSkins.js hace fetch del accounts.json remoto
#      (raw.githubusercontent.com) y lo fusiona con el empaquetado.
#
# Requisitos:
#   pip install discord.py requests
#   Variables de entorno (o edita las constantes abajo):
#     MFSB_TOKEN       â†’ token del bot (Discord Developer Portal)
#     MFSB_GH_TOKEN    â†’ token de GitHub con permiso contents:write
#     MFSB_REPO        â†’ owner/repo  (default EstebanGrp/mfaccs)
#     MFSB_BRANCH      â†’ rama (default main)
#     MFSB_ADMINS      â†’ ids de Discord separados por coma
#     MFSB_CHANNEL     â†’ id del canal autorizado (opcional)
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
    print("[SkinBot] Falta discord.py  â†’  pip install discord.py")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("[SkinBot] Falta requests  â†’  pip install requests")
    sys.exit(1)

# â”€â”€ configuraciÃ³n â”€â”€
TOKEN = os.environ.get("MFSB_TOKEN", "")
GH_TOKEN = os.environ.get("MFSB_GH_TOKEN", "")
REPO = os.environ.get("MFSB_REPO", "EstebanGrp/mfaccs")
BRANCH = os.environ.get("MFSB_BRANCH", "main")
ACCOUNTS_PATH = "accounts.json"
ADMINS = [s.strip() for s in os.environ.get("MFSB_ADMINS", "").split(",") if s.strip()]
CHANNEL_ID = os.environ.get("MFSB_CHANNEL", "")
LOG_CHANNEL_ID = os.environ.get("MFSB_LOG_CHANNEL", "1549572492434346015")

# DB local de cuentas MiniFeather (NO va al repo pÃºblico: contraseÃ±as)
#   ai/mf_accounts.json  â†’ { "cuentas": { "<usuario>": {...} } }
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mf_accounts.json")

GH_API = f"https://api.github.com/repos/{REPO}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{ACCOUNTS_PATH}"
SKINS_DIR = "skins"  # repo pÃºblico: mfaccs/skins/<user>.png
SKINS_RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SKINS_DIR}"

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
            f"ðŸ†• New MiniFeather account: **{username}** â€” created by {creator.mention}\n{msg}"
        )
    except Exception as e:
        warn("notify_new_account fallÃ³:", repr(e))


def gh_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# â”€â”€ GitHub: leer / escribir accounts.json â”€â”€
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
    except json.JSONDecodeError as e:
        warn("accounts.json remoto corrupto:", e)
        return None, sha
    return data, sha


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
    return r.json().get("commit", {}).get("html_url", "")


def gh_upload_png(user, png_bytes):
    """Sube skins/<user>.png al repo pÃºblico. Devuelve la URL raw."""
    path = f"{SKINS_DIR}/{user}.png"
    # sha previo si ya existÃ­a (para reemplazo)
    sha = None
    r = requests.get(f"{GH_API}/contents/{path}?ref={BRANCH}", headers=gh_headers(), timeout=15)
    if r.status_code == 200:
        sha = r.json().get("sha")
    payload = {
        "message": f"skin upload: {user}",
        "content": base64.b64encode(png_bytes).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(f"{GH_API}/contents/{path}", headers=gh_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")
    return f"{SKINS_RAW}/{user}.png"


EXTERNAL_RE = re.compile(r"^https?://(?!raw\.githubusercontent\.com)", re.I)


def rehost_external_skin(user, url):
    """Descarga un PNG de una URL externa (minecraftskins.com, etc.) y lo
    re-sube al repo propio. AsÃ­ el client lo carga desde raw.githubusercontent
    (que SÃ permite CORS) en vez de chocar con el hotlink-block del origen.
    Devuelve la URL raw, o None si la descarga fallÃ³."""
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
        warn("rehost fallÃ³:", repr(e))
        return None


def validate_skin_value(value):
    """Mismas reglas que normalizeSkinValue de CustomSkins.js."""
    v = (value or "").strip()
    if not v:
        return False, "vacÃ­o"
    if v.startswith("custom:"):
        return True, "id custom del client"
    if re.match(r"^(https?://|chrome-extension://|file://|data:image/|blob:)", v, re.I):
        return True, "URL absoluta"
    if "/" in v:
        return True, "ruta /skins/"
    if SKIN_RE.match(v):
        return True, "id vanilla"
    return False, "formato no reconocido"


# â”€â”€ cuentas MiniFeather (local, con contraseÃ±as hasheadas) â”€â”€
USER_RE = re.compile(r"^[a-z0-9_]{3,16}$", re.I)

# En GitHub Actions el disco es efÃ­mero: el archivo de cuentas vive en un
# REPO PRIVADO y se sincroniza por la API de contents (igual que skins).
#   MFSB_ACC_REPO  â†’ owner/repo privado (ej. EstebanGrp/mfaccs-private)
#   MFSB_ACC_PATH  â†’ nombre del archivo (default mf_accounts.json)
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
          "hash": "<pbkdf2$iter$salt$dk>",   # contraseÃ±a
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
            warn("acc_download fallÃ³, sigo con lo local:", e)
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


# â”€â”€ bot de Discord â”€â”€
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
    warn(f"SkinBot listo como {bot.user} â€” repo {REPO}@{BRANCH}")
    warn(f"canal autorizado: {CHANNEL_ID or '(todos)'} | admins: {len(ADMINS) or '(todos)'}")
    # listener de creaciones de cuenta desde el client (ntfy)
    bot.loop.create_task(ntfy_client_accounts_loop())


# â”€â”€ creaciones de cuenta desde el client (ntfy) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                    sdata.setdefault("players", {})[key] = {"skin": skin}
                    gh_upload(sdata, sha, f"skinbot: client create {key}")
        except Exception as e:
            warn("client create: skin inicial fallÃ³:", repr(e))
    try:
        ch = bot.get_channel(int(LOG_CHANNEL_ID)) or await bot.fetch_channel(int(LOG_CHANNEL_ID))
        await ch.send(f"ðŸ†• Account **{username}** created from the MiniFeather Client")
    except Exception as e:
        warn("notify client create fallÃ³:", repr(e))
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
    page="pÃ¡gina para list",
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
        # â”€â”€ acciones sin descargar â”€â”€
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

        # â”€â”€ acciones con jugador â”€â”€
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
                    await interaction.followup.send("seturl exige http(s)://â€¦")
                    return
            else:
                ok, why = validate_skin_value(value)
                if not ok:
                    await interaction.followup.send(f"Skin invÃ¡lida ({why}).")
                    return
            # re-host de URLs externas (CORS desde miniblox.io)
            rehosted = False
            if EXTERNAL_RE.match(value):
                raw = rehost_external_skin(key, value)
                if raw:
                    value, rehosted = raw, True
                else:
                    await interaction.followup.send(
                        "No pude descargar esa imagen (Â¿es un link directo a PNG?). "
                        "Sube el archivo con /skinupload.")
                    return
            old = players.get(key, {}).get("skin")
            players[key] = {"skin": value}
            commit = gh_upload(data, sha, f"skinbot: set {key} = {value[:40]}")
            oldinfo = f" (antes: `{old}`)" if old and old != value else ""
            rh = "\nRe-hosteada en nuestro repo (sin CORS)." if rehosted else ""
            await interaction.followup.send(
                f"OK â€” `{key}` ({kdisp}) â†’ `{value}`{oldinfo}{rh}\n{commit}")

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
            lines = [f"**DB de skins** â€” {total} entradas (pÃ¡g {page}/{pages}):"]
            for k, v in chunk:
                sv = (v or {}).get("skin", "?")
                if len(sv) > 42:
                    sv = sv[:39] + "â€¦"
                lines.append(f"`{k}` â†’ {sv}")
            await interaction.followup.send("\n".join(lines))

        else:
            await interaction.followup.send(
                "AcciÃ³n desconocida. Usa set / seturl / remove / list / reload / sync.")

    except Exception as e:
        warn("comando fallÃ³:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


@tree.command(name="skinupload", description="Upload your own skin PNG â€” it becomes your in-game skin")
@app_commands.describe(image="Square or 2:1 PNG skin file (64-2048px, power of 2)")
async def skinupload_cmd(interaction: discord.Interaction, image: discord.Attachment):
    if not in_channel(interaction):
        await interaction.response.send_message("Unauthorized channel.", ephemeral=True)
        return
    # pÃºblico: cualquiera del canal puede subir la SUYA (requiere cuenta vinculada)
    await interaction.response.defer(ephemeral=True)

    try:
        # 1. debe tener cuenta MiniFeather vinculada
        did = str(interaction.user.id)
        recs = load_local_accounts().get("cuentas", {})
        mine = [u for u, r in recs.items() if r.get("discord_id") == did]
        if not mine:
            await interaction.followup.send(
                "You need a MiniFeather account first â€” use the panel's **Create account** button."
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
        data.setdefault("players", {})[user] = {"skin": raw_url}
        commit = gh_upload(data, sha, f"skinbot: skinupload {user}")
        await interaction.followup.send(
            f"Skin uploaded â€” `{user}` â†’ `{raw_url}`\nVisible in-game in â‰¤5 min.\n{commit}"
        )

    except Exception as e:
        warn("skinupload fallÃ³:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


@tree.command(name="mfaccount", description="Cuentas MiniFeather con contraseÃ±a, vinculadas a Discord")
@app_commands.describe(
    action="create / link / unlink / passwd / info / list / delete",
    username="nombre de la cuenta MiniFeather",
    password="contraseÃ±a (para create/passwd)",
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

    # â”€â”€ CREATE: cualquiera puede crear su cuenta â”€â”€
    if act == "create":
        if not username or not password:
            await interaction.response.send_message(
                "Uso: /mfaccount create username:pepito password:â€¢â€¢â€¢", ephemeral=True)
            return
        if not USER_RE.match(username):
            await interaction.response.send_message(
                "El usuario debe ser 3-16 chars [a-z0-9_].", ephemeral=True)
            return
        if len(password) < 6:
            await interaction.response.send_message(
                "ContraseÃ±a mÃ­nima: 6 caracteres.", ephemeral=True)
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
            "La contraseÃ±a vive hasheada (PBKDF2) solo en la PC del bot.", ephemeral=True)
        return

    # â”€â”€ acciones que requieren dueÃ±o o admin â”€â”€
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
                await interaction.followup.send("ContraseÃ±a mÃ­nima: 6 caracteres.")
                return
            cuentas[key]["hash"] = hash_password(password)
            save_local_accounts(data)
            await interaction.followup.send(f"ContraseÃ±a de `{key}` actualizada.")

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
                     f"Discord: {f'<@{did}>' if did else 'â€”'}",
                     f"Creada: <t:{entry.get('created', 0)}:R>",
                     f"Creador: <@{entry.get('creator', '0')}>"]
            await interaction.followup.send("\n".join(lines))

        elif act == "list":
            if not cuentas:
                await interaction.followup.send("Sin cuentas aÃºn.")
                return
            lines = ["**Cuentas MiniFeather** â€” %d:" % len(cuentas)]
            for name, c in sorted(cuentas.items()):
                did = c.get("discord_id")
                who = f"<@{did}>" if did else "sin vincular"
                lines.append(f"`{name}` â†’ {who}")
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
                "AcciÃ³n desconocida. create / link / unlink / passwd / info / list / delete")

    except Exception as e:
        warn("mfaccount fallÃ³:", repr(e))
        try:
            await interaction.followup.send(f"Error: {e}")
        except Exception:
            pass


# â”€â”€ /panel: GUI with buttons + modals â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
            f"Account **{username}** created and linked to {interaction.user.mention} âœ…",
            ephemeral=True)

    async def on_error(self, interaction, error):
        warn("modal create fallÃ³:", repr(error))
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
            # miniblox.io â†’ re-host en nuestro repo antes de guardar
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
            players[key] = {"skin": value}
            commit = gh_upload(sdata, sha, f"skinbot: panel set {key} = {value[:40]}")
            oldinfo = f" (before: `{old}`)" if old and old != value else ""
            rh = "\nRe-hosted in our repo (CORS-safe)." if rehosted else ""
            send = interaction.followup.send if rehosted else interaction.response.send_message
            await send(
                f"Skin of **{key}** â†’ `{value}`{oldinfo}{rh}\nVisible in-game within 5 min.{f'  {commit}' if commit else ''}",
                ephemeral=True)
        except Exception as e:
            warn("panel skin fallÃ³:", repr(e))
            try:
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)
            except Exception:
                pass

    async def on_error(self, interaction, error):
        warn("modal skin fallÃ³:", repr(error))
        try:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)
        except Exception:
            pass


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(label="Create account", style=discord.ButtonStyle.green,
                       emoji="ðŸª¶", custom_id="mfsb:crear")
    async def crear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        await interaction.response.send_modal(CreateAccountModal())

    @discord.ui.button(label="Set skin", style=discord.ButtonStyle.blurple,
                       emoji="ðŸŽ¨", custom_id="mfsb:skin")
    async def skin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not in_channel(interaction):
            await interaction.response.send_message("Channel not authorized.", ephemeral=True)
            return
        await interaction.response.send_modal(SetSkinModal())

    @discord.ui.button(label="My account", style=discord.ButtonStyle.gray,
                       emoji="â„¹ï¸", custom_id="mfsb:info")
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
            f"Discord: {f'<@{did}>' if did else 'â€”'}\n"
            f"Created: <t:{entry.get('created', 0)}:R>\n"
            f"Creator: <@{entry.get('creator', '0')}>",
            ephemeral=True)


PANEL_EMBED = discord.Embed(
    title="ðŸª¶ MiniFeather â€” Accounts & Skins",
    description=(
        "Create your MiniFeather account (password-protected, linked to your Discord) "
        "and choose the skin others will see in-game.\n\n"
        "**Upload your own skin:** use `/skinupload` with a PNG attached "
        "(square or 2:1, 64â€“2048px).\n\n"
        "**Skin formats (Set skin):**\n"
        "`custom:mf_...` â€” client custom id\n"
        "`chris`, `bob` â€” Miniblox vanilla\n"
        "`devs/itzesteban` â€” pack path\n"
        "`https://...png` â€” absolute URL"
    ),
    color=0x5865F2,
)
PANEL_EMBED.set_footer(text="Skin changes reach the game within 5 minutes")


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
    warn(f"repo: {REPO}@{BRANCH} Â· path: {ACCOUNTS_PATH}")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
