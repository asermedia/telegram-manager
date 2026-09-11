import asyncio
import io
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import qrcode
from qrcode.image.svg import SvgPathImage
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise RuntimeError("BOT_TOKEN, API_ID and API_HASH must be set in .env")

API_ID = int(API_ID)

BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "telethon_sessions"
SESSION_DIR.mkdir(exist_ok=True)

# One lightweight Telethon client handles the bot UI.
bot = TelegramClient(str(BASE_DIR / "bot_session"), API_ID, API_HASH)

# User-account clients are created only for users who connect their Telegram account.
clients = {}
login_tasks = {}
sender_tasks = {}

# Per-user UI/settings state. Session files themselves persist on disk.
states = {}


def state_for(user_id):
    return states.setdefault(
        user_id,
        {
            "mode": None,
            "groups": [],
            "selected": set(),
            "repeat": 1,
            "delay": 10,
        },
    )


def main_menu():
    return [
        [Button.inline("🔐 Connect Telegram", b"connect")],
        [Button.inline("📋 My Groups", b"groups"),
         Button.inline("➕ Join Group", b"join")],
        [Button.inline("🚪 Leave Group", b"leave")],
        [Button.inline("📨 Message Sender", b"sender")],
        [Button.inline("🔌 Disconnect", b"disconnect")],
    ]


async def get_client(user_id):
    client = clients.get(user_id)

    if client is None:
        session_path = SESSION_DIR / f"user_{user_id}"
        client = TelegramClient(str(session_path), API_ID, API_HASH)
        clients[user_id] = client

    if not client.is_connected():
        await client.connect()

    if not await client.is_user_authorized():
        return None

    return client


async def send_menu(event, text="Choose an option:"):
    await event.respond(text, buttons=main_menu())


async def require_account(event):
    client = await get_client(event.sender_id)
    if client is None:
        await event.respond(
            "❌ Your Telegram account isn't connected yet.\n\n"
            "Tap 🔐 Connect Telegram to log in with QR."
        )
        return None
    return client


async def qr_login(user_id):
    # Avoid two QR login tasks for the same user.
    if user_id in login_tasks and not login_tasks[user_id].done():
        return

    async def _login():
        session_path = SESSION_DIR / f"user_{user_id}"
        client = clients.get(user_id)
        if client is None:
            client = TelegramClient(str(session_path), API_ID, API_HASH)
            clients[user_id] = client

        try:
            await client.connect()

            if await client.is_user_authorized():
                me = await client.get_me()
                await bot.send_message(
                    user_id,
                    f"✅ Already connected as {me.first_name or 'Telegram user'} "
                    f"(ID: {me.id}).",
                    buttons=main_menu(),
                )
                return

            qr = await client.qr_login()

            # Generate QR locally and send it to the Telegram user.
            qr_code = qrcode.QRCode(box_size=8, border=4)
            qr_code.add_data(qr.url)
            qr_code.make(fit=True)
            img = qr_code.make_image(image_factory=SvgPathImage)
            svg = img.to_string()
            if isinstance(svg, str):
                svg = svg.encode("utf-8")
            buf = io.BytesIO(svg)
            buf.seek(0)

            await bot.send_file(
                user_id,
                buf,
                file_name="telegram_login_qr.svg",
                caption=(
                    "📱 Scan this QR with Telegram:\n"
                    "Settings → Devices → Link Desktop Device\n\n"
                    "The QR expires shortly. If it expires, tap "
                    "🔐 Connect Telegram again."
                ),
            )

            try:
                await qr.wait()
            except SessionPasswordNeededError:
                await bot.send_message(
                    user_id,
                    "🔐 Your Telegram account has 2-step verification enabled.\n"
                    "Send your Telegram 2FA password here to finish login.",
                )
                state_for(user_id)["mode"] = "awaiting_2fa"
                return

            me = await client.get_me()
            name = " ".join(x for x in [me.first_name, me.last_name] if x) or "Unknown"

            await bot.send_message(
                user_id,
                f"🎉 Telegram connected successfully!\n\n"
                f"Name: {name}\n"
                f"ID: {me.id}\n\n"
                "Your Telegram account is now connected.",
                buttons=main_menu(),
            )

        except Exception as e:
            await bot.send_message(
                user_id,
                f"❌ Telegram login failed:\n`{type(e).__name__}: {e}`",
                parse_mode="md",
                buttons=main_menu(),
            )
        finally:
            login_tasks.pop(user_id, None)

    login_tasks[user_id] = asyncio.create_task(_login())


async def show_groups(event):
    client = await require_account(event)
    if client is None:
        return

    st = state_for(event.sender_id)
    dialogs = await client.get_dialogs()
    groups = []

    for d in dialogs:
        entity = d.entity
        if isinstance(entity, Chat) or (
            isinstance(entity, Channel) and entity.megagroup
        ):
            groups.append((entity.id, d.name or "Unnamed group"))

    st["groups"] = groups

    if not groups:
        await event.respond("📋 No groups/supergroups found.", buttons=main_menu())
        return

    # Keep selection valid after refreshing the list.
    valid_ids = {gid for gid, _ in groups}
    st["selected"].intersection_update(valid_ids)

    rows = []
    for i, (gid, name) in enumerate(groups[:80]):
        mark = "✅" if gid in st["selected"] else "⬜"
        label = f"{mark} {name[:45]}"
        rows.append([Button.inline(label, f"sel:{i}".encode())])

    rows.append([Button.inline("🔄 Refresh", b"groups")])
    rows.append([Button.inline("🏠 Main Menu", b"menu")])

    await event.respond(
        f"📋 <b>My Groups</b>\n"
        f"Found: {len(groups)}\n"
        f"Selected: {len(st['selected'])}\n\n"
        "Tap groups to select/deselect them.",
        buttons=rows,
        parse_mode="html",
    )


async def join_group(event, text):
    client = await require_account(event)
    if client is None:
        return

    value = text.strip()

    try:
        if "t.me/+" in value or "t.me/joinchat/" in value:
            invite_hash = value.rstrip("/").split("/")[-1]
            await client(ImportChatInviteRequest(invite_hash))
        else:
            username = value.replace("https://t.me/", "").replace("http://t.me/", "")
            username = username.strip("@/ ")
            entity = await client.get_entity(username)
            await client(JoinChannelRequest(entity))

        await event.respond("✅ Successfully joined the group/channel.", buttons=main_menu())
    except Exception as e:
        await event.respond(
            f"❌ Couldn't join it:\n`{type(e).__name__}: {e}`",
            parse_mode="md",
            buttons=main_menu(),
        )


async def leave_group(event, text):
    client = await require_account(event)
    if client is None:
        return

    value = text.strip()
    try:
        value = value.replace("https://t.me/", "").replace("http://t.me/", "")
        value = value.strip("@/ ")
        entity = await client.get_entity(value)
        await client.delete_dialog(entity)
        await event.respond("✅ Left the group/channel.", buttons=main_menu())
    except Exception as e:
        await event.respond(
            f"❌ Couldn't leave it:\n`{type(e).__name__}: {e}`",
            parse_mode="md",
            buttons=main_menu(),
        )


async def sender_menu(event):
    client = await require_account(event)
    if client is None:
        return

    st = state_for(event.sender_id)
    text = (
        "📨 <b>Message Sender</b>\n\n"
        f"Selected groups: <b>{len(st['selected'])}</b>\n"
        f"Repeat count: <b>{st['repeat']}</b>\n"
        f"Delay: <b>{st['delay']} seconds</b>\n\n"
        "The sender uses your latest message from Saved Messages "
        "and sends it only to the groups you explicitly selected."
    )

    buttons = [
        [Button.inline("🎯 Select Groups", b"groups")],
        [Button.inline("🔢 Set Repeat Count", b"setrepeat")],
        [Button.inline("⏱ Set Delay", b"setdelay")],
        [Button.inline("▶️ Start", b"startsend"),
         Button.inline("⏹ Stop", b"stopsend")],
        [Button.inline("🏠 Main Menu", b"menu")],
    ]
    await event.respond(text, buttons=buttons, parse_mode="html")


async def stop_sender(user_id):
    task = sender_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def start_sender(user_id):
    client = await get_client(user_id)
    if client is None:
        await bot.send_message(user_id, "❌ Connect your Telegram account first.")
        return

    st = state_for(user_id)

    if not st["selected"]:
        await bot.send_message(
            user_id,
            "❌ Select at least one group first.",
            buttons=main_menu(),
        )
        return

    if sender_tasks.get(user_id) and not sender_tasks[user_id].done():
        await bot.send_message(user_id, "⚠️ Sender is already running.")
        return

    async def _send():
        try:
            # Get the latest Saved Messages message once when the job starts.
            saved = await client.get_messages("me", limit=1)
            if not saved:
                await bot.send_message(user_id, "❌ Saved Messages is empty.")
                return

            message = saved[0]
            await bot.send_message(
                user_id,
                f"▶️ Sender started.\n"
                f"Groups: {len(st['selected'])}\n"
                f"Repeats: {st['repeat']}\n"
                f"Delay: {st['delay']} seconds",
            )

            group_ids = list(st["selected"])

            for repeat_no in range(1, st["repeat"] + 1):
                for group_id in group_ids:
                    try:
                        entity = await client.get_entity(group_id)
                        await client.forward_messages(entity, message)
                    except FloodWaitError as e:
                        await bot.send_message(
                            user_id,
                            f"⏳ Telegram requested a {e.seconds}s wait. "
                            "Pausing before continuing.",
                        )
                        await asyncio.sleep(e.seconds)
                    except Exception as e:
                        await bot.send_message(
                            user_id,
                            f"⚠️ Couldn't send to {group_id}: "
                            f"{type(e).__name__}: {e}",
                        )

                if repeat_no < st["repeat"]:
                    await asyncio.sleep(st["delay"])

            await bot.send_message(user_id, "✅ Sender finished.")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            await bot.send_message(
                user_id,
                f"❌ Sender stopped because of an error:\n"
                f"`{type(e).__name__}: {e}`",
                parse_mode="md",
            )
        finally:
            sender_tasks.pop(user_id, None)

    sender_tasks[user_id] = asyncio.create_task(_send())


async def disconnect_user(user_id):
    await stop_sender(user_id)

    client = clients.pop(user_id, None)
    if client:
        try:
            if client.is_connected():
                await client.log_out()
        except Exception:
            pass
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

    session = SESSION_DIR / f"user_{user_id}.session"
    journal = SESSION_DIR / f"user_{user_id}.session-journal"

    for path in (session, journal):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    states.pop(user_id, None)


@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    state_for(event.sender_id)["mode"] = None
    await send_menu(
        event,
        "🤖 <b>Telegram Manager</b>\n\nChoose what you want to do:",
    )


@bot.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    data = event.data.decode(errors="ignore")
    st = state_for(user_id)

    await event.answer()

    if data == "menu":
        st["mode"] = None
        await event.edit("🤖 <b>Telegram Manager</b>\n\nChoose an option:", buttons=main_menu(), parse_mode="html")

    elif data == "connect":
        st["mode"] = None
        await event.edit("🔐 Generating your Telegram login QR...")
        await qr_login(user_id)

    elif data == "groups":
        st["mode"] = None
        await event.edit("📋 Loading your groups...")
        await show_groups(event)

    elif data.startswith("sel:"):
        try:
            idx = int(data.split(":", 1)[1])
            if 0 <= idx < len(st["groups"]):
                gid, _ = st["groups"][idx]
                if gid in st["selected"]:
                    st["selected"].remove(gid)
                else:
                    st["selected"].add(gid)
        except (ValueError, IndexError):
            pass
        await show_groups(event)

    elif data == "join":
        st["mode"] = "join"
        await event.edit(
            "➕ <b>Join Group</b>\n\n"
            "Send a public @username, t.me link, or invite link (t.me/+...).",
            buttons=[[Button.inline("❌ Cancel", b"menu")]],
            parse_mode="html",
        )

    elif data == "leave":
        st["mode"] = "leave"
        await event.edit(
            "🚪 <b>Leave Group</b>\n\n"
            "Send the group's @username or t.me link.",
            buttons=[[Button.inline("❌ Cancel", b"menu")]],
            parse_mode="html",
        )

    elif data == "sender":
        st["mode"] = None
        await event.edit("📨 Loading sender...")
        await sender_menu(event)

    elif data == "setrepeat":
        st["mode"] = "repeat"
        await event.edit(
            "🔢 Send the repeat count (1–100).",
            buttons=[[Button.inline("❌ Cancel", b"sender")]],
        )

    elif data == "setdelay":
        st["mode"] = "delay"
        await event.edit(
            "⏱ Send the delay in seconds (10–86400).",
            buttons=[[Button.inline("❌ Cancel", b"sender")]],
        )

    elif data == "startsend":
        st["mode"] = None
        await event.edit("▶️ Starting sender...")
        await start_sender(user_id)

    elif data == "stopsend":
        st["mode"] = None
        await stop_sender(user_id)
        await event.edit("⏹ Sender stopped.", buttons=main_menu())

    elif data == "disconnect":
        st["mode"] = None
        await disconnect_user(user_id)
        await event.edit(
            "🔌 Telegram account disconnected and its local session was removed.",
            buttons=main_menu(),
        )


@bot.on(events.NewMessage)
async def text_handler(event):
    if not event.is_private or event.out:
        return

    text = (event.raw_text or "").strip()
    if text.startswith("/start"):
        return

    user_id = event.sender_id
    st = state_for(user_id)
    mode = st.get("mode")

    if mode == "awaiting_2fa":
        client = clients.get(user_id)
        if client is None:
            st["mode"] = None
            await event.respond("❌ Login session expired. Tap Connect Telegram again.", buttons=main_menu())
            return

        try:
            await client.sign_in(password=text)
            st["mode"] = None
            me = await client.get_me()
            await event.respond(
                f"🎉 Telegram connected successfully!\n\n"
                f"Name: {me.first_name or 'Unknown'}\n"
                f"ID: {me.id}",
                buttons=main_menu(),
            )
        except Exception as e:
            await event.respond(
                f"❌ 2FA login failed:\n`{type(e).__name__}: {e}`",
                parse_mode="md",
            )
        return

    if mode == "join":
        st["mode"] = None
        await join_group(event, text)
        return

    if mode == "leave":
        st["mode"] = None
        await leave_group(event, text)
        return

    if mode == "repeat":
        try:
            value = int(text)
            if not 1 <= value <= 100:
                raise ValueError
            st["repeat"] = value
            st["mode"] = None
            await event.respond(f"✅ Repeat count set to {value}.", buttons=main_menu())
        except ValueError:
            await event.respond("❌ Enter a whole number from 1 to 100.")
        return

    if mode == "delay":
        try:
            value = int(text)
            if not 10 <= value <= 86400:
                raise ValueError
            st["delay"] = value
            st["mode"] = None
            await event.respond(f"✅ Delay set to {value} seconds.", buttons=main_menu())
        except ValueError:
            await event.respond("❌ Enter seconds from 10 to 86400.")
        return


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram Manager is running")

    def log_message(self, format, *args):
        return


def start_health_server():
    # Render provides PORT dynamically. Bind to 0.0.0.0 so the service is reachable.
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server listening on 0.0.0.0:{port}")
    server.serve_forever()


async def main():
    # Render Web Services require an HTTP listener on the assigned PORT.
    threading.Thread(target=start_health_server, daemon=True).start()

    await bot.start(bot_token=BOT_TOKEN)
    print("Telegram Manager bot is running.")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
