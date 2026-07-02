import threading
import asyncio
import re
import os
import time
import telebot
from supabase import create_client, Client
from telebot import types
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ===================== CONFIG =====================
# Render pe deploy karte time ye sab values code me hardcode NAHI karni —
# Render dashboard ke "Environment" tab me set karna hai. Local testing ke
# liye fallback bhi diya hai taaki bina env vars ke bhi chal jaye.

API_ID = int(os.environ.get('API_ID', '0'))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STR = os.environ.get('SESSION_STR', '')

TARGET_BOT = os.environ.get('TARGET_BOT', '@apna_coder_key_bot')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')

FORCE_SUB_CHANNELS = ['@ModappsKing', '@EduAppsKing']

ADMIN_IDS = [
    int(x) for x in os.environ.get('ADMIN_IDS', '5350926991').split(',') if x.strip()
]

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

# Render free web service ko PORT pe bind karna zaroori hai warna
# deploy fail/sleep ho jata hai. Render khud PORT env var deta hai.
PORT = int(os.environ.get('PORT', '10000'))
# ====================================================

bot = telebot.TeleBot(BOT_TOKEN)
loop = asyncio.new_event_loop()
client = TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH, loop=loop)

processing_lock = threading.Lock()
last_target_msg = None
captured_msg = None
response_event = None


# ================= DATABASE LAYER (Supabase) =================
# Supabase = cloud Postgres DB -> bot kahi bhi host ho (Render, Railway, etc),
# data persistent rahega even disk wipe / redeploy ho jaye.

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
USERS_TABLE = 'users'

# Supabase SQL Editor me ye table pehle bana lena (one-time):
#
# create table users (
#     user_id     bigint primary key,
#     username    text,
#     first_name  text,
#     verified    boolean default false,
#     joined_at   text,
#     verified_at text
# );


def add_or_update_user(user):
    """Naya user ho to insert, purana ho to username/name update (upsert)."""
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    existing = (
        supabase.table(USERS_TABLE)
        .select('user_id')
        .eq('user_id', user.id)
        .execute()
    )

    if existing.data:
        supabase.table(USERS_TABLE).update({
            'username': user.username or "",
            'first_name': user.first_name or "",
        }).eq('user_id', user.id).execute()
    else:
        supabase.table(USERS_TABLE).insert({
            'user_id': user.id,
            'username': user.username or "",
            'first_name': user.first_name or "",
            'verified': False,
            'joined_at': now,
        }).execute()


def is_verified_in_db(user_id):
    res = (
        supabase.table(USERS_TABLE)
        .select('verified')
        .eq('user_id', user_id)
        .execute()
    )
    if res.data:
        return bool(res.data[0]['verified'])
    return False


def mark_verified(user_id):
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    supabase.table(USERS_TABLE).update({
        'verified': True,
        'verified_at': now,
    }).eq('user_id', user_id).execute()


def get_stats():
    total_res = (
        supabase.table(USERS_TABLE)
        .select('user_id', count='exact')
        .execute()
    )
    verified_res = (
        supabase.table(USERS_TABLE)
        .select('user_id', count='exact')
        .eq('verified', True)
        .execute()
    )
    total = total_res.count or 0
    verified = verified_res.count or 0
    unverified = total - verified
    return total, verified, unverified


def get_all_user_ids():
    res = supabase.table(USERS_TABLE).select('user_id').execute()
    return [row['user_id'] for row in res.data]
# ====================================================


# ================= FORCE-SUB CHECK =================
def is_member_of_channel(user_id, channel):
    try:
        member = bot.get_chat_member(channel, user_id)
        return member.status in ('member', 'administrator', 'creator')
    except Exception as e:
        print(f"[force-sub check error] {channel}: {e}")
        # Agar bot channel me admin nahi hai ya channel galat hai to error aayega.
        # Safe default: not joined treat karo taaki silently bypass na ho.
        return False


def check_all_channels(user_id):
    not_joined = []
    for ch in FORCE_SUB_CHANNELS:
        if not is_member_of_channel(user_id, ch):
            not_joined.append(ch)
    return not_joined


def send_force_join_message(chat_id, not_joined):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in not_joined:
        ch_username = ch.lstrip('@')
        markup.add(types.InlineKeyboardButton(
            f"📢 Join {ch}", url=f"https://t.me/{ch_username}"
        ))
    markup.add(types.InlineKeyboardButton("✅ I've Joined", callback_data="verify_join"))
    bot.send_message(
        chat_id,
        "⚠️ *Bot use karne ke liye neeche diye channels join karna zaroori hai!*\n\n"
        "Join karne ke baad *✅ I've Joined* button par click karo.",
        reply_markup=markup,
        parse_mode="Markdown"
    )
# ====================================================


def clean_target_response(text):
    if not text:
        return text
    return text


def build_inline_markup(telethon_msg):
    if not telethon_msg or not telethon_msg.buttons:
        return None

    markup = types.InlineKeyboardMarkup(row_width=1)
    for row in telethon_msg.buttons:
        row_buttons = []
        for btn in row:
            row_buttons.append(
                types.InlineKeyboardButton(
                    text=btn.text,
                    callback_data=f"cb_{btn.text[:30]}"
                )
            )
        markup.row(*row_buttons)
    return markup


def setup_telethon_handlers():
    global response_event, captured_msg
    response_event = asyncio.Event()

    @client.on(events.NewMessage(chats=TARGET_BOT))
    @client.on(events.MessageEdited(chats=TARGET_BOT))
    async def incoming_target_handler(event):
        global captured_msg, response_event

        if event.out:
            return

        text = event.message.text or ""

        if "fetching" in text.lower() or "loading" in text.lower():
            return

        captured_msg = event.message
        response_event.set()


def start_telethon_thread():
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(client.connect())
        if not loop.run_until_complete(client.is_user_authorized()):
            print("[FATAL] SESSION_STR invalid/expired! Naya generate karo.")
            return
    except Exception as e:
        print(f"[FATAL] Telethon connect failed: {e}")
        return
    setup_telethon_handlers()
    print("Userbot Active...")
    loop.run_forever()


threading.Thread(target=start_telethon_thread, daemon=True).start()


async def _send_text_to_target(text):
    global captured_msg, response_event
    response_event.clear()
    captured_msg = None

    try:
        await client.send_message(TARGET_BOT, text)
    except Exception as e:
        print(f"[ERROR] send_message to target failed: {e}")
        return None

    try:
        await asyncio.wait_for(response_event.wait(), timeout=30.0)
        return captured_msg
    except asyncio.TimeoutError:
        return None


async def _click_target_button(row, col):
    global last_target_msg, captured_msg, response_event
    if not last_target_msg or not last_target_msg.buttons:
        return None

    try:
        response_event.clear()
        captured_msg = None

        btn = last_target_msg.buttons[row][col]

        if hasattr(btn, 'url') and btn.url:
            import urllib.parse
            parsed = urllib.parse.urlparse(btn.url)
            params = urllib.parse.parse_qs(parsed.query)
            if 'start' in params:
                start_value = params['start'][0]
                await client.send_message(TARGET_BOT, f"/start {start_value}")
                await asyncio.wait_for(response_event.wait(), timeout=30.0)
                return captured_msg
        elif hasattr(btn, 'data') and btn.data:
            await btn.click()
            await asyncio.wait_for(response_event.wait(), timeout=30.0)
            return captured_msg
        else:
            await client.send_message(TARGET_BOT, "/start")
            await asyncio.wait_for(response_event.wait(), timeout=30.0)
            return captured_msg

        return None
    except asyncio.TimeoutError:
        return None
    except Exception as e:
        return None


def _proceed_with_target_bot(chat_id):
    """Target bot ko /start bhejo aur response user tak relay karo."""
    if not client.is_connected():
        bot.send_message(chat_id, "❌ Userbot connected nahi hai. Thodi der baad try karo.")
        return

    try:
        future = asyncio.run_coroutine_threadsafe(_send_text_to_target("/start"), loop)
        res_msg = future.result(timeout=40)
    except Exception as e:
        print(f"[ERROR] _proceed_with_target_bot: {e}")
        bot.send_message(chat_id, "❌ Error hua, /start dobara try karo.")
        return

    if res_msg:
        global last_target_msg
        last_target_msg = res_msg
        markup = build_inline_markup(res_msg)
        clean_text = clean_target_response(res_msg.text)
        bot.send_message(
            chat_id,
            clean_text or "Welcome!",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        bot.send_message(chat_id, "❌ Target bot not responding.")


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ================= /start (force-sub + db) =================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_or_update_user(message.from_user)

    not_joined = check_all_channels(message.from_user.id)
    if not_joined:
        send_force_join_message(message.chat.id, not_joined)
        return

    mark_verified(message.from_user.id)
    _proceed_with_target_bot(message.chat.id)


# ================= verify-join callback =================
@bot.callback_query_handler(func=lambda call: call.data == "verify_join")
def handle_verify_join(call):
    add_or_update_user(call.from_user)
    user_id = call.from_user.id
    not_joined = check_all_channels(user_id)

    if not_joined:
        bot.answer_callback_query(
            call.id, "❌ Abhi tak sabhi channels join nahi kiye!", show_alert=True
        )
        return

    mark_verified(user_id)
    bot.answer_callback_query(call.id, "✅ Verified! Bot ready ho gaya...")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    _proceed_with_target_bot(call.message.chat.id)


# ================= verification gate decorator =================
def require_verification(get_user, get_chat_id):
    """
    Decorator factory: kisi bhi handler ko force-sub verified gate ke peeche
    laga deta hai. get_user / get_chat_id functions batate hain ki
    message ya call object se user/chat id kaise nikalna hai.
    """
    def decorator(func):
        def wrapper(obj):
            user = get_user(obj)
            chat_id = get_chat_id(obj)
            add_or_update_user(user)

            not_joined = check_all_channels(user.id)
            if not_joined:
                send_force_join_message(chat_id, not_joined)
                return
            if not is_verified_in_db(user.id):
                mark_verified(user.id)
            return func(obj)
        return wrapper
    return decorator


# ================= original cb_ button flow (verification gated) =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("cb_"))
@require_verification(
    get_user=lambda call: call.from_user,
    get_chat_id=lambda call: call.message.chat.id
)
def handle_callback_query(call):
    global last_target_msg

    try:
        bot.answer_callback_query(call.id, "Processing...")
    except Exception:
        pass

    button_text = call.data[3:]

    with processing_lock:
        status_msg = bot.send_message(call.message.chat.id, "⏳ Processing...")

        button_found = False
        if last_target_msg and last_target_msg.buttons:
            for row_idx, row in enumerate(last_target_msg.buttons):
                for col_idx, btn in enumerate(row):
                    if btn.text == button_text:
                        button_found = True

                        future = asyncio.run_coroutine_threadsafe(
                            _click_target_button(row_idx, col_idx), loop
                        )
                        res_msg = future.result()

                        try:
                            bot.delete_message(call.message.chat.id, status_msg.message_id)
                        except Exception:
                            pass

                        if res_msg:
                            last_target_msg = res_msg
                            markup = build_inline_markup(res_msg)
                            clean_text = clean_target_response(res_msg.text)

                            bot.send_message(
                                call.message.chat.id,
                                clean_text or "✅ Done",
                                reply_markup=markup,
                                parse_mode="Markdown"
                            )
                            return
                        else:
                            bot.send_message(
                                call.message.chat.id,
                                "❌ No response from target bot.",
                                parse_mode="Markdown"
                            )
                            return

        if not button_found:
            try:
                bot.delete_message(call.message.chat.id, status_msg.message_id)
            except Exception:
                pass
            bot.send_message(
                call.message.chat.id,
                "❌ Button expired. Use /start again.",
                parse_mode="Markdown"
            )


# ================= ADMIN PANEL =================
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Ye command sirf admin ke liye hai.")
        return

    total, verified, unverified = get_stats()
    text = (
        "👑 *Admin Panel*\n\n"
        f"👥 Total Users: `{total}`\n"
        f"✅ Verified Users: `{verified}`\n"
        f"⏳ Unverified Users: `{unverified}`\n\n"
        "Commands:\n"
        "`/stats` - Quick stats\n"
        "`/broadcast <message>` - Sab verified users ko message bhejo"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(commands=['stats'])
def stats_command(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Ye command sirf admin ke liye hai.")
        return

    total, verified, unverified = get_stats()
    bot.send_message(
        message.chat.id,
        f"👥 Total: {total} | ✅ Verified: {verified} | ⏳ Unverified: {unverified}"
    )


@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Ye command sirf admin ke liye hai.")
        return

    text_to_send = message.text.partition(' ')[2].strip()
    if not text_to_send:
        bot.send_message(message.chat.id, "Use: /broadcast Your message here")
        return

    user_ids = get_all_user_ids()
    sent, failed = 0, 0
    status = bot.send_message(message.chat.id, f"📤 Broadcasting to {len(user_ids)} users...")

    for uid in user_ids:
        try:
            bot.send_message(uid, text_to_send)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)  # rate-limit se bachne ke liye chhota delay

    bot.edit_message_text(
        f"✅ Broadcast complete.\nSent: {sent} | Failed: {failed}",
        message.chat.id,
        status.message_id
    )
# =====================================================


# ================= fallback text handler (verification gated) =================
@bot.message_handler(func=lambda m: True)
@require_verification(
    get_user=lambda message: message.from_user,
    get_chat_id=lambda message: message.chat.id
)
def handle_text_input(message):
    with processing_lock:
        status_msg = bot.send_message(message.chat.id, "⏳ Processing...")

        if not client.is_connected():
            try:
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception:
                pass
            bot.send_message(message.chat.id, "❌ Userbot connected nahi hai abhi.")
            return

        try:
            future = asyncio.run_coroutine_threadsafe(
                _send_text_to_target(message.text), loop
            )
            res_msg = future.result(timeout=40)
        except Exception as e:
            print(f"[ERROR] handle_text_input: {e}")
            try:
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception:
                pass
            bot.send_message(message.chat.id, "❌ Error hua, dobara try karo.")
            return

        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass

        if res_msg:
            global last_target_msg
            last_target_msg = res_msg
            markup = build_inline_markup(res_msg)
            clean_text = clean_target_response(res_msg.text)

            bot.send_message(
                message.chat.id,
                clean_text or "🤖 Done",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        else:
            bot.send_message(message.chat.id, "❌ No response from target bot.")


# ================= DUMMY HTTP SERVER (Render port binding) =================
# Render free Web Service ko ek PORT pe listen karna zaroori hai, warna
# deploy "unhealthy" dikhayega. UptimeRobot isi endpoint ko ping karega
# taaki free instance sleep na ho.

def run_dummy_server():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class PingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Bot is running!")

        def log_message(self, format, *args):
            pass  # console spam band karne ke liye

    server = HTTPServer(('0.0.0.0', PORT), PingHandler)
    print(f"Dummy HTTP server running on port {PORT}")
    server.serve_forever()


threading.Thread(target=run_dummy_server, daemon=True).start()
# ====================================================


if __name__ == "__main__":
    print("Bot is Live!")
    print("Target:", TARGET_BOT)
    print("Force-sub channels:", FORCE_SUB_CHANNELS)

    bot.remove_webhook()
    time.sleep(3)  # purani polling session clear hone do

    while True:
        try:
            print("Polling start...")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except KeyboardInterrupt:
            print("\nBot stopped.")
            loop.stop()
            break
        except Exception as e:
            print(f"[ERROR] Polling crashed: {e}")
            print("15 second me retry karega...")
            time.sleep(15)
