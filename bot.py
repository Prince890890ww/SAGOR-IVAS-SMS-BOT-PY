import telebot
import time
import requests
import re
import html
from bs4 import BeautifulSoup
import threading
from datetime import datetime
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import concurrent.futures
from collections import defaultdict
from con_ns import (
    BOT_TOKEN,
    CHAT_ID,
    ADMIN_ID,
    LOGIN_URL,
    PORTAL_URL,
    SMS_URL,
    EMAIL,
    PASSWORD,
    START_DATE,
    country_codes
)

# ================= আলট্রা-ফাস্ট কনফিগার ================

# ⚡⚡⚡ ফাস্টেস্ট সেটিংস ⚡⚡⚡
POLL_INTERVAL_SECONDS = 1  # ১ সেকেন্ড পরপর চেক (সবচেয়ে দ্রুত)
MAX_WORKERS = 50  # ৫০টি নাম্বার একসাথে চেক করবে
CONNECTION_POOL_SIZE = 100  # ১০০টি কানেকশন পুল

RANGES_DIR = "ranges"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ================= আলট্রা-ফাস্ট সেশন সেটআপ =================

session = requests.Session()

# বিশাল কানেকশন পুল
adapter = requests.adapters.HTTPAdapter(
    pool_connections=CONNECTION_POOL_SIZE,
    pool_maxsize=CONNECTION_POOL_SIZE,
    max_retries=3,
    pool_block=False
)
session.mount('http://', adapter)
session.mount('https://', adapter)

# দ্রুত রিট্রাই স্ট্রাটেজি
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

retry_strategy = Retry(
    total=3,
    backoff_factor=0.1,  # ০.১ সেকেন্ড পরপর রিট্রাই (সবচেয়ে দ্রুত)
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

csrf_token = None
seen_otps = {}
user_states = {}
last_reset_time = time.time()
failed_attempts = defaultdict(int)  # fail ট্র্যাকিং
range_cache = []  # ক্যাশ
last_cache_update = 0

if not os.path.exists(RANGES_DIR):
    os.makedirs(RANGES_DIR)

# ================= ক্যাশ ফাংশন =================

def get_all_numbers_cached():
    """নাম্বার ক্যাশ করে (প্রতি ১০ সেকেন্ড পর আপডেট)"""
    global range_cache, last_cache_update
    now = time.time()
    if now - last_cache_update > 10 or not range_cache:
        range_cache = load_all_numbers()
        last_cache_update = now
    return range_cache

def load_all_numbers():
    all_items = []
    try:
        for fn in os.listdir(RANGES_DIR):
            if fn.endswith(".txt"):
                range_name = fn[:-4].replace("_", " ")
                path = os.path.join(RANGES_DIR, fn)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        nums = [line.strip() for line in f if line.strip()]
                    for n in nums:
                        all_items.append({"number": n, "range": range_name})
                except Exception as e:
                    print(f"File read error {fn}: {e}")
    except Exception as e:
        print(f"Error loading numbers: {e}")
    return all_items

def reset_session_if_needed():
    global session, last_reset_time
    now = time.time()
    if now - last_reset_time > 120:  # ২ মিনিট পর রিসেট
        print("[SESSION] রিসেট করা হচ্ছে...")
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=CONNECTION_POOL_SIZE,
            pool_maxsize=CONNECTION_POOL_SIZE,
            max_retries=3
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        last_reset_time = now
        return True
    return False

def login_and_get_csrf():
    global csrf_token
    try:
        reset_session_if_needed()
        r = session.get(LOGIN_URL, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        token_tag = soup.find("input", {"name": "_token"})
        if not token_tag or "value" not in token_tag.attrs:
            return False
        initial_token = token_tag["value"]

        payload = {"_token": initial_token, "email": EMAIL, "password": PASSWORD}
        r_post = session.post(LOGIN_URL, data=payload, timeout=10)

        if r_post.status_code != 200 or "login" in r_post.url.lower():
            return False

        r_portal = session.get(PORTAL_URL, timeout=10)
        soup_portal = BeautifulSoup(r_portal.text, "html.parser")

        meta = soup_portal.find("meta", {"name": "csrf-token"})
        if meta and "content" in meta.attrs:
            csrf_token = meta["content"]
            return True

        input_tag = soup_portal.find("input", {"name": "_token"})
        if input_tag and "value" in input_tag.attrs:
            csrf_token = input_tag["value"]
            return True

        return False
    except Exception as e:
        print(f"[LOGIN/CSRF ERROR]: {str(e)}")
        return False

def fetch_otps(number, range_name):
    global csrf_token, failed_attempts
    
    # যদি এই নাম্বার ২ বার fail করে, তাহলে কিছুক্ষণ স্কিপ
    if failed_attempts[number] > 2:
        if time.time() - failed_attempts[f"{number}_time"] < 30:
            return None, "Skipping temporarily"
        else:
            failed_attempts[number] = 0

    if not csrf_token and not login_and_get_csrf():
        return None, "লগইন/CSRF সমস্যা"

    payload = {
        "start": START_DATE,
        "end": time.strftime("%Y-%m-%d"),
        "Number": number,
        "Range": range_name
    }

    headers = {
        "X-CSRF-TOKEN": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": PORTAL_URL,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate"
    }

    try:
        r = session.post(SMS_URL, data=payload, headers=headers, timeout=10)

        if r.status_code == 419:
            if login_and_get_csrf():
                headers["X-CSRF-TOKEN"] = csrf_token
                r = session.post(SMS_URL, data=payload, headers=headers, timeout=10)
            else:
                return None, "419 - CSRF fail"

        if r.status_code != 200:
            failed_attempts[number] += 1
            failed_attempts[f"{number}_time"] = time.time()
            return None, f"HTTP {r.status_code}"

        soup = BeautifulSoup(r.text, "html.parser")
        sms_cards = soup.find_all('div', class_=lambda v: v and 'card-body' in v.split())

        messages = []
        for card in sms_cards:
            p = card.select_one('p.mb-0.pb-0')
            if not p: continue
            raw = p.get_text(separator=" ", strip=True)
            full_text = html.unescape(raw).strip()
            if len(full_text) < 5: continue

            otp_patterns = [
                r'(?:code|OTP|কোড|ওটিপি|Your WhatsApp code)[:\s-]*(\d{3,8}(?:-\d{3})?)',
                r'(\d{3,8}(?:-\d{3})?)\s*(?:is your|your code|code|OTP)',
                r'\b(\d{3,8}(?:-\d{3})?)\b'
            ]

            otp = None
            for pat in otp_patterns:
                m = re.search(pat, full_text, re.IGNORECASE)
                if m:
                    otp = m.group(1).replace("-", "")
                    break

            if otp:
                messages.append({"otp": otp, "full_body": full_text})
            else:
                messages.append({"otp": None, "full_body": full_text})

        # সফল হলে fail count রিসেট
        if number in failed_attempts:
            del failed_attempts[number]

        return messages, None

    except Exception as e:
        failed_attempts[number] += 1
        failed_attempts[f"{number}_time"] = time.time()
        return None, str(e)

def detect_service(text):
    """দ্রুত সার্ভিস ডিটেকশন"""
    lower = text.lower()
    
    services = {
        "whatsapp": ["whatsapp", "wa"],
        "telegram": ["telegram", "tg"],
        "facebook": ["facebook", "fb"],
        "instagram": ["instagram", "ig"],
        "google": ["google", "gmail"],
        "twitter": ["twitter", "x.com"],
        "tiktok": ["tiktok"],
        "snapchat": ["snapchat"],
        "amazon": ["amazon"],
        "netflix": ["netflix"],
        "spotify": ["spotify"],
        "discord": ["discord"],
        "steam": ["steam"],
        "binance": ["binance"],
        "paypal": ["paypal"],
        "uber": ["uber"],
        "pathao": ["pathao"],
        "foodpanda": ["foodpanda"],
        "bkash": ["bkash"],
        "nagad": ["nagad"]
    }
    
    for service, keywords in services.items():
        for kw in keywords:
            if kw in lower:
                return service.capitalize()
    return "Other Service"

def fetch_and_post_new_otps(number, range_name):
    msgs, err = fetch_otps(number, range_name)
    if err or not msgs:
        return

    new_msgs = []
    for msg in msgs:
        key = f"{number}:{msg['otp']}"
        if key not in seen_otps:
            seen_otps[key] = time.time()
            new_msgs.append(msg)

    if not new_msgs:
        return

    # দেশ ডিটেক্ট
    country_name = "Unknown"
    flag = "🏍"
    clean_num = number.lstrip("+0")
    for length in [3, 2, 1]:
        prefix = clean_num[:length]
        if prefix in country_codes:
            country_name, flag = country_codes[prefix]
            break

    hidden_num = number[:4] + "★★★" + number[-4:] if len(number) >= 8 else number

    for msg in new_msgs:
        otp = msg['otp']
        otp_text = otp if otp else "❌ OTP NOT FOUND"
        full_body = msg['full_body']
        client = detect_service(full_body)

        safe_body = html.escape(full_body).replace("#", "\\#").replace("<", "&lt;").replace(">", "&gt;")

        message_text = f"""🔩🔩. <b>{flag} {client.upper()} 🅰🅷 🅼🅴🆃🅷🅾🅳 </b>.🔪🔪
﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐﹐
<blockquote>{flag} 𝗖𝗼𝘂𝗻𝘁𝗿𝘆 » {country_name}
☎️ 𝗡𝘂𝗺𝗯𝗲𝗿 » {hidden_num}</blockquote>
🔑𝗢𝗧𝗣 » <code>{otp_text}</code>
<blockquote><code>{safe_body}</code></blockquote>

power by AH METHOD TEAM"""

        markup = InlineKeyboardMarkup()
        markup.row_width = 3
        button1 = InlineKeyboardButton("📢 NUMBER CHANNEL", url="https://t.me/blackotpnum")
        button2 = InlineKeyboardButton("💬 CHAT GROUP", url="https://t.me/EarningHub6112")
        button3 = InlineKeyboardButton("🤖 NUMBER BOT", url="https://t.me/ah_method_number_bot")
        markup.add(button1, button2, button3)

        try:
            bot.send_message(CHAT_ID, message_text, reply_markup=markup)
            print(f"[SENT] {client} OTP {otp} for {number}")
        except Exception as e:
            print(f"[SEND ERR] {number}: {e}")

def polling_loop():
    print(f"[🚀] আলট্রা-ফাস্ট বট চালু হয়েছে!")
    print(f"[⚡] পোলিং: প্রতি {POLL_INTERVAL_SECONDS} সেকেন্ড")
    print(f"[🔥] ওয়ার্কার: {MAX_WORKERS}")
    print(f"[💪] কানেকশন পুল: {CONNECTION_POOL_SIZE}")
    
    consecutive_errors = 0
    cycle_count = 0
    
    while True:
        cycle_start = time.time()
        cycle_count += 1
        
        try:
            items = get_all_numbers_cached()
            count = len(items)

            if count > 0:
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = [executor.submit(fetch_and_post_new_otps, item["number"], item["range"]) for item in items]
                    concurrent.futures.wait(futures, timeout=5)
                consecutive_errors = 0

            elapsed = time.time() - cycle_start
            sleep_time = max(POLL_INTERVAL_SECONDS - elapsed, 0.1)
            
            if sleep_time > 0:
                time.sleep(sleep_time)

        except Exception as ex:
            consecutive_errors += 1
            print(f"[ERR] #{consecutive_errors}: {ex}")
            if consecutive_errors > 5:
                time.sleep(10)
                consecutive_errors = 0
            else:
                time.sleep(2)

# ================= অ্যাডমিন প্যানেল =================

def get_range_buttons():
    markup = InlineKeyboardMarkup(row_width=2)
    for fn in os.listdir(RANGES_DIR):
        if fn.endswith(".txt"):
            range_name = fn[:-4].replace("_", " ")
            markup.add(InlineKeyboardButton(range_name, callback_data=f"upload_{range_name}"))
    markup.add(InlineKeyboardButton("➕ ADD New Range", callback_data="add_range"))
    return markup

def get_delete_buttons():
    markup = InlineKeyboardMarkup(row_width=2)
    for fn in os.listdir(RANGES_DIR):
        if fn.endswith(".txt"):
            range_name = fn[:-4].replace("_", " ")
            markup.add(InlineKeyboardButton(f"🗑️ {range_name}", callback_data=f"delete_{range_name}"))
    markup.add(InlineKeyboardButton("« Back", callback_data="back_to_menu"))
    return markup

@bot.message_handler(commands=["start"])
def start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "বট চালু আছে।")
        return
    markup = get_range_buttons()
    bot.reply_to(message, "<b>⚡ আলট্রা-ফাস্ট অ্যাডমিন প্যানেল</b>\nরেঞ্জ সিলেক্ট করো:", reply_markup=markup)

@bot.message_handler(commands=["delete"])
def delete_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    if not os.listdir(RANGES_DIR):
        bot.reply_to(message, "কোনো রেঞ্জ নেই।")
        return
    markup = get_delete_buttons()
    bot.reply_to(message, "<b>যে রেঞ্জ ডিলিট করতে চাও সিলেক্ট করো:</b>", reply_markup=markup)

@bot.message_handler(commands=["get"])
def manual_get(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.reply_to(message, "ম্যানুয়াল চেক শুরু...")
    items = load_all_numbers()
    for item in items:
        fetch_and_post_new_otps(item["number"], item["range"])
    bot.reply_to(message, f"চেক শেষ ({len(items)} নম্বর)।")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "শুধু অ্যাডমিন!", show_alert=True)
        return

    if call.data == "add_range":
        user_states[call.from_user.id] = {"state": "waiting_range_name"}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "নতুন রেঞ্জের নাম দাও (যেমন: BENIN 379)")

    elif call.data.startswith("upload_"):
        range_name = call.data.replace("upload_", "")
        user_states[call.from_user.id] = {"state": "waiting_file", "range_name": range_name}
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"এখন '{range_name}' এর জন্য TXT ফাইল আপলোড করো।")

    elif call.data.startswith("delete_"):
        range_name = call.data.replace("delete_", "")
        safe_fn = range_name.replace(" ", "_").replace("/", "-") + ".txt"
        path = os.path.join(RANGES_DIR, safe_fn)
        if os.path.exists(path):
            os.remove(path)
            bot.answer_callback_query(call.id, f"'{range_name}' ডিলিট হয়েছে!", show_alert=True)
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text="রেঞ্জ ডিলিট সফল।", reply_markup=get_range_buttons())
        else:
            bot.answer_callback_query(call.id, "রেঞ্জ পাওয়া যায়নি!", show_alert=True)

    elif call.data == "back_to_menu":
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                              text="<b>⚡ আলট্রা-ফাস্ট অ্যাডমিন প্যানেল</b>", reply_markup=get_range_buttons())

@bot.message_handler(func=lambda m: m.from_user.id in user_states and user_states[m.from_user.id].get("state") == "waiting_range_name")
def handle_range_name(message):
    range_name = message.text.strip()
    if not range_name:
        bot.reply_to(message, "নাম দিতে হবে।")
        return

    safe_fn = range_name.replace(" ", "_").replace("/", "-") + ".txt"
    path = os.path.join(RANGES_DIR, safe_fn)

    created = False
    if not os.path.exists(path):
        open(path, 'a', encoding='utf-8').close()
        created = True

    msg = f"রেঞ্জ '{range_name}' {'তৈরি হয়েছে' if created else 'আগে থেকেই আছে'}!\n\n"
    msg += f"এখন '{range_name}' এর জন্য TXT ফাইল আপলোড করো।"
    bot.reply_to(message, msg)

    user_states[message.from_user.id] = {"state": "waiting_file", "range_name": range_name}

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "শুধু অ্যাডমিন!")
        return

    if not message.document.file_name.lower().endswith('.txt'):
        bot.reply_to(message, "শুধু .txt ফাইল!")
        return

    if message.from_user.id not in user_states or user_states[message.from_user.id].get("state") != "waiting_file":
        bot.reply_to(message, "প্রথমে রেঞ্জ সিলেক্ট / ADD করো।")
        return

    range_name = user_states[message.from_user.id]["range_name"]
    safe_fn = range_name.replace(" ", "_").replace("/", "-") + ".txt"
    path = os.path.join(RANGES_DIR, safe_fn)

    if not os.path.exists(path):
        bot.reply_to(message, f"রেঞ্জ '{range_name}' পাওয়া যায়নি!")
        del user_states[message.from_user.id]
        return

    file_info = bot.get_file(message.document.file_id)
    downloaded = bot.download_file(file_info.file_path)
    new_nums = [line.strip() for line in downloaded.decode('utf-8').splitlines() if line.strip()]

    existing = set()
    if os.path.getsize(path) > 0:
        with open(path, 'r', encoding='utf-8') as f:
            existing = set(line.strip() for line in f if line.strip())

    added = 0
    with open(path, 'a', encoding='utf-8') as f:
        for num in new_nums:
            if num not in existing:
                f.write(num + '\n')
                existing.add(num)
                added += 1

    bot.reply_to(message, f"সফল! '{range_name}' এ {added} টি নতুন নম্বর যোগ হয়েছে।")

    del user_states[message.from_user.id]
    bot.send_message(message.chat.id, "রেঞ্জ লিস্ট আপডেট:", reply_markup=get_range_buttons())

if __name__ == "__main__":
    if not login_and_get_csrf():
        print("Initial login failed — চেক করো")

    threading.Thread(target=polling_loop, daemon=True).start()

    print("\n" + "="*50)
    print("⚡⚡⚡ আলট্রা-ফাস্ট বট চালু ⚡⚡⚡")
    print("="*50)
    print(f"📊 পোলিং: প্রতি {POLL_INTERVAL_SECONDS} সেকেন্ড")
    print(f"🚀 ওয়ার্কার: {MAX_WORKERS}")
    print(f"🔌 কানেকশন পুল: {CONNECTION_POOL_SIZE}")
    print(f"📱 মোট নাম্বার: {len(load_all_numbers())}")
    print("="*50)

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=60)
        except Exception as e:
            print(f"[CRASH] {e}")
            time.sleep(5)