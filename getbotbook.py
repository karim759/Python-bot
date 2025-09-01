# ebooks_bot.py with PostgreSQL + Keep-Alive Custom Status Page
import os
import time
import traceback
import threading
from datetime import datetime, timezone
import telebot
from telebot import types, apihelper
from flask import Flask
import psycopg2

# ================= CONFIG =================
API_TOKEN = os.getenv("API_TOKEN")            # from Render env vars
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))    # from Render env vars
SPECIAL_PIN = os.getenv("SPECIAL_PIN", "2762")
DELETE_DELAY = 60
FILES_PER_PAGE = 5
DB_URL = os.getenv("DATABASE_URL")            # PostgreSQL URL from Render

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

# ================= KEEP-ALIVE SERVER =================
app = Flask('')
start_time = time.time()

@app.route('/')
def home():
    uptime = int(time.time() - start_time)
    hours, rem = divmod(uptime, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"""
    <html>
      <head><title>📚 Telegram Bot Status</title></head>
      <body style="font-family: Arial; text-align: center; margin-top: 50px;">
        <h1>🤖 E-Books Telegram Bot</h1>
        <p style="font-size:18px;">✅ Bot is running!</p>
        <p>⏱️ Uptime: {hours}h {minutes}m {seconds}s</p>
        <p>🌐 Powered by Render</p>
      </body>
    </html>
    """

def run_flask():
    app.run(host="0.0.0.0", port=8080)

# ================= DB HELPERS =================
def log(s: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {s}")

def safe_connect():
    return psycopg2.connect(DB_URL, sslmode="require")

def safe_execute(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return True
    except Exception:
        print("[DB ERROR] executing:", sql, params)
        traceback.print_exc()
        return False

def ensure_schema():
    conn = safe_connect(); cur = conn.cursor()
    safe_execute(cur, """
        CREATE TABLE IF NOT EXISTS files (
            id SERIAL PRIMARY KEY,
            file_id TEXT,
            title TEXT,
            tags TEXT,
            special INTEGER DEFAULT 0,
            uploader BIGINT,
            approved INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    safe_execute(cur, """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            allowed_special INTEGER DEFAULT 0
        )
    """)
    conn.commit(); conn.close()

ensure_schema()

# ================= HELPERS =================
user_states = {}

def now_iso(): return datetime.now(timezone.utc).isoformat()

def schedule_delete(chat_id, message_id):
    def worker():
        time.sleep(DELETE_DELAY)
        try: bot.delete_message(chat_id, message_id)
        except Exception as e: log(f"[AUTO-DELETE ERROR] {e}")
    threading.Thread(target=worker, daemon=True).start()

def live_countdown(chat_id, message_id, title, delay):
    def worker():
        remaining = delay
        while remaining > 0:
            try:
                bot.edit_message_text(
                    f"📖 {title}\n⏳ This file will expire in {remaining}s",
                    chat_id, message_id
                )
            except: pass
            step = 5 if remaining >= 5 else remaining
            time.sleep(step); remaining -= step
        try: bot.edit_message_text("⏳ File expired & removed.", chat_id, message_id)
        except: pass
    threading.Thread(target=worker, daemon=True).start()

def main_menu_for(user_id):
    conn=safe_connect(); cur=conn.cursor()
    cur.execute("SELECT allowed_special FROM users WHERE user_id=%s",(user_id,))
    row=cur.fetchone(); conn.close()
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📂 List Files","🔍 Search Files")
    if row and row[0]==1: kb.add("🔒 Special Files")
    return kb

# ================= ADMIN APPROVE/REJECT =================
@bot.callback_query_handler(func=lambda c:c.data.startswith("approve_"))
def cb_approve(call):
    try:
        fid=int(call.data.split("_",1)[1])
        conn=safe_connect(); cur=conn.cursor()
        safe_execute(cur,"UPDATE files SET approved=1 WHERE id=%s",(fid,))
        conn.commit(); conn.close()
        bot.answer_callback_query(call.id,"Approved ✅")
        try: bot.edit_message_caption("File Approved ✅",call.message.chat.id,call.message.message_id)
        except: pass
        log(f"Admin approved file id={fid}")
    except: traceback.print_exc()

@bot.callback_query_handler(func=lambda c:c.data.startswith("reject_"))
def cb_reject(call):
    try:
        fid=int(call.data.split("_",1)[1])
        conn=safe_connect(); cur=conn.cursor()
        safe_execute(cur,"DELETE FROM files WHERE id=%s",(fid,))
        conn.commit(); conn.close()
        bot.answer_callback_query(call.id,"Rejected ❌")
        try: bot.edit_message_caption("File Rejected ❌",call.message.chat.id,call.message.message_id)
        except: pass
        log(f"Admin rejected file id={fid}")
    except: traceback.print_exc()

# ================= UPLOAD =================
@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_states[message.chat.id]={"mode":"upload","step":"title",
        "file_id":message.document.file_id,"uploader":message.from_user.id}
    bot.reply_to(message,"📖 Please enter a title for this file:")

@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("step")=="title")
def upload_title(message):
    st=user_states.get(message.chat.id,{}); st['title']=message.text.strip() or "Untitled"
    st['step']="tags"; user_states[message.chat.id]=st
    bot.reply_to(message,"🏷️ Enter tags (comma separated, optional):")

@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("step")=="tags")
def upload_tags(message):
    st=user_states.get(message.chat.id,{}); st['tags']=message.text.strip()
    st['step']="special"; user_states[message.chat.id]=st
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Yes",callback_data="special_yes"),
           types.InlineKeyboardButton("❌ No",callback_data="special_no"))
    bot.send_message(message.chat.id,"🔒 Is this a special file?",reply_markup=kb)

@bot.callback_query_handler(func=lambda c:c.data in ("special_yes","special_no"))
def upload_special_choice(call):
    st=user_states.get(call.message.chat.id); 
    if not st: return
    try:
        is_special=(call.data=="special_yes" and call.from_user.id==ADMIN_ID)
        conn=safe_connect(); cur=conn.cursor()
        safe_execute(cur,"INSERT INTO files (file_id,title,tags,special,uploader,approved,created_at) VALUES (%s,%s,%s,%s,%s,0,%s)",
            (st['file_id'],st['title'],st['tags'],1 if is_special else 0,st['uploader'],now_iso()))
        conn.commit(); cur.execute("SELECT MAX(id) FROM files"); new_id=cur.fetchone()[0]; conn.close()
        user_states.pop(call.message.chat.id,None)
        bot.send_message(call.message.chat.id,"✅ File submitted for admin approval.")
        kb=types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Approve",callback_data=f"approve_{new_id}"),
               types.InlineKeyboardButton("❌ Reject",callback_data=f"reject_{new_id}"))
        bot.send_document(ADMIN_ID,st['file_id'],caption=f"📥 New Upload\n<b>{st['title']}</b>",reply_markup=kb)
    except: traceback.print_exc()

# ================= LIST FILES =================
def render_list(chat_id,page=0,special=False):
    conn=safe_connect(); cur=conn.cursor()
    cur.execute("SELECT id,title,special FROM files WHERE approved=1 AND special=%s ORDER BY id DESC",(1 if special else 0,))
    rows=cur.fetchall(); conn.close()
    if not rows: bot.send_message(chat_id,"📂 No files."); return
    start,end=page*FILES_PER_PAGE,(page+1)*FILES_PER_PAGE
    kb=types.InlineKeyboardMarkup()
    for fid,title,sp in rows[start:end]:
        kb.add(types.InlineKeyboardButton(f"{title}{' 🔒' if sp else ''}",callback_data=f"get_{fid}"))
        if chat_id==ADMIN_ID:
            kb.add(types.InlineKeyboardButton("🗑️ Remove",callback_data=f"delete_{fid}"))
    nav=[]
    if start>0: nav.append(types.InlineKeyboardButton("⬅️ Prev",callback_data=f"page_{page-1}_{int(special)}"))
    if end<len(rows): nav.append(types.InlineKeyboardButton("➡️ Next",callback_data=f"page_{page+1}_{int(special)}"))
    if nav: kb.row(*nav)
    bot.send_message(chat_id,f"📚 Files (Page {page+1})",reply_markup=kb)

@bot.message_handler(func=lambda m:m.text=="📂 List Files")
def handle_list(message): render_list(message.chat.id,0,False)

@bot.message_handler(func=lambda m:m.text=="🔒 Special Files")
def handle_special(message): render_list(message.chat.id,0,True)

@bot.callback_query_handler(func=lambda c:c.data.startswith("page_"))
def cb_page(call):
    _,p,sp=call.data.split("_")
    render_list(call.message.chat.id,int(p),bool(int(sp)))

# ================= SEARCH =================
@bot.message_handler(func=lambda m:m.text=="🔍 Search Files")
def search_prompt(message):
    user_states[message.chat.id]={"mode":"search"}
    bot.send_message(message.chat.id,"🔎 Enter a keyword:")

@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("mode")=="search")
def run_search(message):
    q=message.text.strip().lower(); user_states.pop(message.chat.id,None)
    conn=safe_connect(); cur=conn.cursor()
    cur.execute("SELECT id,title,special,tags FROM files WHERE approved=1")
    rows=cur.fetchall(); conn.close()
    results=[r for r in rows if q in (r[1] or "").lower() or q in (r[3] or "").lower()]
    if not results: bot.send_message(message.chat.id,"❌ No results."); return
    kb=types.InlineKeyboardMarkup()
    for fid,title,sp,_ in results[:50]:
        kb.add(types.InlineKeyboardButton(f"{title}{' 🔒' if sp else ''}",callback_data=f"get_{fid}"))
    bot.send_message(message.chat.id,"🔍 Results:",reply_markup=kb)

# ================= GET FILE =================
@bot.callback_query_handler(func=lambda c:c.data.startswith("get_"))
def cb_get_file(call):
    try:
        fid=int(call.data.split("_",1)[1])
        conn=safe_connect(); cur=conn.cursor()
        cur.execute("SELECT file_id,title,special FROM files WHERE id=%s",(fid,))
        row=cur.fetchone(); conn.close()

        if not row:
            log(f"[MISSING FILE] Requested id={fid} by {call.from_user.id}")
            bot.answer_callback_query(call.id,"⚠️ File not found in database.")
            bot.send_message(call.message.chat.id,"❌ Sorry, this file is no longer available.")
            return

        file_id,title,sp=row
        if sp:
            conn=safe_connect(); cur=conn.cursor()
            cur.execute("SELECT allowed_special FROM users WHERE user_id=%s",(call.from_user.id,))
            a=cur.fetchone(); conn.close()
            if not (a and a[0]==1): 
                bot.answer_callback_query(call.id,"🚫 Not allowed.")
                return

        try: bot.delete_message(call.message.chat.id,call.message.message_id)
        except: pass

        status=bot.send_message(call.message.chat.id,f"📖 Preparing download: {title} ...")

        try:
            sent=bot.send_document(call.message.chat.id,file_id,caption=f"📖 {title}")
            live_countdown(call.message.chat.id,status.message_id,title,DELETE_DELAY)
            schedule_delete(call.message.chat.id,sent.message_id)
        except Exception as e:
            log(f"[FILE SEND ERROR] {e}")
            bot.edit_message_text("❌ File could not be sent. It may have been deleted or is unavailable.",
                call.message.chat.id,status.message_id)

    except Exception as e:
        traceback.print_exc()
        bot.send_message(call.message.chat.id,"⚠️ Unexpected error occurred. Please try again later.")

# ================= DELETE FILE =================
@bot.callback_query_handler(func=lambda c:c.data.startswith("delete_"))
def cb_delete(call):
    if call.from_user.id!=ADMIN_ID: 
        bot.answer_callback_query(call.id,"🚫 Not authorized."); return
    fid=int(call.data.split("_",1)[1])
    conn=safe_connect(); cur=conn.cursor()
    safe_execute(cur,"DELETE FROM files WHERE id=%s",(fid,))
    conn.commit(); conn.close()
    bot.answer_callback_query(call.id,"🗑️ File deleted.")
    try: bot.delete_message(call.message.chat.id,call.message.message_id)
    except: pass

# ================= START & PIN =================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    conn=safe_connect(); cur=conn.cursor()
    safe_execute(cur,"INSERT INTO users (user_id,allowed_special) VALUES (%s,0) ON CONFLICT (user_id) DO NOTHING",(message.from_user.id,))
    conn.commit(); conn.close()
    welcome=("📚 <b>Welcome to the E-Books & Subject Books Sharing Bot</b>!\n\n"
             "📖 Download books\n🔍 Search by title/tags\n📤 Upload your own files (sent for admin approval)\ncontact admin @kexerbot")
    sent=bot.send_message(message.chat.id,welcome,reply_markup=main_menu_for(message.from_user.id))
    try: bot.pin_chat_message(message.chat.id,sent.message_id,disable_notification=True)
    except: pass

@bot.message_handler(commands=["kexer"])
def cmd_kexer(message):
    user_states[message.chat.id]={"mode":"pin"}
    bot.reply_to(message,"🔑 Enter the special PIN:")

@bot.message_handler(func=lambda m:user_states.get(m.chat.id,{}).get("mode")=="pin")
def handle_pin(message):
    if message.text.strip()==SPECIAL_PIN:
        conn=safe_connect(); cur=conn.cursor()
        safe_execute(cur,"INSERT INTO users (user_id,allowed_special) VALUES (%s,1) ON CONFLICT (user_id) DO UPDATE SET allowed_special=1",(message.from_user.id,))
        conn.commit(); conn.close()
        user_states.pop(message.chat.id,None)
        bot.send_message(message.chat.id,"✅ PIN accepted!",reply_markup=main_menu_for(message.from_user.id))
    else: bot.reply_to(message,"❌ Wrong PIN.")

# ================= POLLING with AUTO-RECONNECT =================
def reset_http_session():
    try: apihelper._req_session=None
    except: pass

def start_polling_loop():
    backoff=1
    while True:
        try:
            reset_http_session()
            log("🚀 Bot polling started...")
            bot.infinity_polling(timeout=30,long_polling_timeout=20,skip_pending=True)
        except Exception as e:
            log(f"[POLL ERROR] {type(e)} {e}")
            time.sleep(backoff)
            backoff=min(backoff*2,120)
            log(f"[RETRY] reconnecting in {backoff} seconds...")

# ================= MAIN =================
if __name__=="__main__":
    threading.Thread(target=run_flask,daemon=True).start()
    start_polling_loop()
