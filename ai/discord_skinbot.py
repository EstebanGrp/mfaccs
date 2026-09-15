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
import os
import re
import secrets
import sys
import time

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

# DB local de cuentas MiniFeather (NO va al repo público: contraseñas)
#   ai/mf_accounts.json  → { "cuentas": { "<usuario>": {...} } }
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mf_accounts.json")

GH_API = f"https://api.github.com/repos/{REPO}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{ACCOUNTS_PATH}"
SKINS_DIR = "skins"  # repo público: mfaccs/skins/<user>.png
SKINS_RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SKINS_DIR}"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SKIN_RE = re.compile(r"^[a-z0-9_]+$", re.I)


def warn(*a):
    print("[SkinBot]", *a, file=sys.stderr)


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
    """Sube skins/<user>.png al repo público. Devuelve la URL raw."""
    path = f"{SKINS_DIR}/{user}.png"
    # sha previo si ya existía (para reemplazo)
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
    await tree.sync()
    warn(f"SkinBot listo como {bot.user} — repo {REPO}@{BRANCH}")
    warn(f"canal autorizado: {CHANNEL_ID or '(todos)'} | admins: {len(ADMINS) or '(todos)'}")


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
            old = players.get(key, {}).get("skin")
            players[key] = {"skin": value}
            commit = gh_upload(data, sha, f"skinbot: set {key} = {value[:40]}")
            oldinfo = f" (antes: `{old}`)" if old and old != value else ""
            await interaction.followup.send(
                f"OK — `{key}` ({kdisp}) → `{value}`{oldinfo}\n{commit}")

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
        data.setdefault("players", {})[user] = {"skin": raw_url}
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
            sdata, sha = gh_download()
            if sdata is None:
                await interaction.response.send_message(
                    "Remote accounts.json is corrupt.", ephemeral=True)
                return
            players = sdata.setdefault("players", {})
            old = players.get(key, {}).get("skin")
            players[key] = {"skin": value}
            commit = gh_upload(sdata, sha, f"skinbot: panel set {key} = {value[:40]}")
            oldinfo = f" (before: `{old}`)" if old and old != value else ""
            await interaction.response.send_message(
                f"Skin of **{key}** → `{value}`{oldinfo}\nVisible in-game within 5 min.{f'  {commit}' if commit else ''}",
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


PANEL_EMBED = discord.Embed(
    title="🪶 MiniFeather — Accounts & Skins",
    description=(
        "Create your MiniFeather account (password-protected, linked to your Discord) "
        "and choose the skin others will see in-game.\n\n"
        "**Upload your own skin:** use `/skinupload` with a PNG attached "
        "(square or 2:1, 64–2048px).\n\n"
        "**Skin formats (Set skin):**\n"
        "`custom:mf_...` — client custom id\n"
        "`chris`, `bob` — Miniblox vanilla\n"
        "`devs/itzesteban` — pack path\n"
        "`https://...png` — absolute URL"
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
    warn(f"repo: {REPO}@{BRANCH} · path: {ACCOUNTS_PATH}")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
