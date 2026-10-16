import asyncio
import datetime
import logging
import os
import sqlite3
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from flask import Flask

# Keep-alive web server for Render Web Service to avoid Timeouts
app = Flask(__name__)

@app.route('/')
def home():
    return "RoMi Star Board Master Edition is running 24/7 online!"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip().isdigit()}
DB_PATH = os.getenv('DB_PATH', 'romi.db')

# Safe timezone setup
BD_TZ = datetime.timezone(datetime.timedelta(hours=6))

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
log = logging.getLogger('romi_star_board')

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

# Database Tables Setup
conn.execute('''CREATE TABLE IF NOT EXISTS reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT, reviewed_at TEXT,
 record_bonus INTEGER NOT NULL DEFAULT 0, photo_id TEXT DEFAULT '')''')

conn.execute('''CREATE TABLE IF NOT EXISTS members (
 user_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '')''')

conn.execute('''CREATE TABLE IF NOT EXISTS reset_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
 target_day TEXT NOT NULL DEFAULT '', cutoff_at TEXT NOT NULL, admin_id INTEGER NOT NULL)''')

conn.execute('''CREATE TABLE IF NOT EXISTS personal_records (
 user_id INTEGER PRIMARY KEY, best_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '')''')

conn.execute('''CREATE TABLE IF NOT EXISTS manual_adjustments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, points INTEGER NOT NULL,
 reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''')

conn.execute('''CREATE TABLE IF NOT EXISTS missing_accounts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL, withdraw_no INTEGER NOT NULL, missing_count INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(user_id, withdraw_no) ON CONFLICT REPLACE)''')

conn.execute('''CREATE TABLE IF NOT EXISTS settings (
 key TEXT PRIMARY KEY, value TEXT NOT NULL)''')

conn.commit()

USER_MENU = ReplyKeyboardMarkup([
 [KeyboardButton('📝 Submit Report'), KeyboardButton('💎 My Points')],
 [KeyboardButton('📌 My Missing Accounts'), KeyboardButton('🏆 Leaderboard')],
 [KeyboardButton('📊 Weekly Board'), KeyboardButton('👤 My Profile')],
 [KeyboardButton('❓ Help')]
], resize_keyboard=True, is_persistent=True)

ADMIN_MENU = ReplyKeyboardMarkup([
 [KeyboardButton('📝 Submit Report'), KeyboardButton('💎 My Points')],
 [KeyboardButton('📌 My Missing Accounts'), KeyboardButton('🏆 Leaderboard')],
 [KeyboardButton('📊 Weekly Board'), KeyboardButton('👤 My Profile')],
 [KeyboardButton('⚡ Admin Panel'), KeyboardButton('❓ Help')]
], resize_keyboard=True, is_persistent=True)

def menu_for(uid): return ADMIN_MENU if is_admin(uid) else USER_MENU

def now(): return datetime.datetime.now(BD_TZ)
def today(): return now().date().isoformat()
def is_admin(uid): return uid in ADMIN_IDS

def get_badge(pts):
    if pts >= 1000: return '👑 Legendary King'
    if pts >= 500:  return '💎 Diamond Elite'
    if pts >= 300:  return '🔥 Elite Master'
    if pts >= 150:  return '🥇 Gold Pro'
    if pts >= 50:   return '🥈 Silver Star'
    return '🥉 Novice Fighter'

def save_member(user):
    conn.execute('''INSERT INTO members(user_id,name,username) VALUES(?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username''',
      (user.id, (user.full_name or 'Member')[:50], user.username or ''))
    conn.commit()

def name_of(uid):
    r = conn.execute('SELECT name,username FROM members WHERE user_id=?', (uid,)).fetchone()
    if not r: return f'Member {uid}'
    return '@'+r['username'] if r['username'] else (r['name'] or f'Member {uid}')

def cutoff(scope, target_day=''):
    r = conn.execute('SELECT cutoff_at FROM reset_log WHERE scope=? AND target_day=? ORDER BY id DESC LIMIT 1', (scope,target_day)).fetchone()
    return r['cutoff_at'] if r else None

def get_multiplier():
    row = conn.execute("SELECT value FROM settings WHERE key='multiplier'").fetchone()
    return float(row['value']) if row else 1.0

def manual_pts_for(uid, cut=None):
    sql = "SELECT SUM(points) FROM manual_adjustments WHERE user_id=?"
    args = [uid]
    if cut:
        sql += " AND created_at > ?"
        args.append(cut)
    res = conn.execute(sql, args).fetchone()[0]
    return int(res or 0)

def rows_for(uid, scope='all'):
    sql = "SELECT day,count,created_at,record_bonus FROM reports WHERE user_id=? AND status='approved'"
    args = [uid]
    if scope == 'daily':
        d = today(); sql += ' AND day=?'; args.append(d); cut = cutoff('daily', d)
    elif scope == 'weekly': cut = cutoff('weekly')
    else: cut = cutoff('all')
    if cut:
        sql += ' AND created_at>?'; args.append(cut)
    sql += ' ORDER BY day,created_at'
    return conn.execute(sql,args).fetchall(), cut

def score(rows, uid=None, scope='all', cut=None):
    points = 0; total_acc = 0; streak = 0; prev = None; best = 0
    mult = get_multiplier()
    for r in rows:
        d = datetime.date.fromisoformat(r['day']) if len(r['day'])==10 else datetime.datetime.fromisoformat(r['day']).date()
        n = int(r['count'])
        total_acc += n
        base_earned = (n + int(r['record_bonus'] or 0)) * mult
        points += base_earned
        best = max(best, n)
        if n >= 50:
            points += 2 * mult
            streak = streak + 1 if prev and (d - prev).days == 1 else 1
            if streak in (3, 5, 7): points += streak * mult
        else:
            streak = 0
        prev = d
    if uid:
        points += manual_pts_for(uid, cut)
    return {'points': int(points), 'accounts': total_acc, 'best': best, 'days': len(rows), 'streak': streak}

def lifetime_record(uid):
    r = conn.execute('SELECT best_count FROM personal_records WHERE user_id=?', (uid,)).fetchone()
    if r: return int(r['best_count'])
    m = conn.execute("SELECT MAX(count) AS m FROM reports WHERE user_id=? AND status='approved'", (uid,)).fetchone()['m'] or 0
    conn.execute('INSERT OR IGNORE INTO personal_records(user_id,best_count,updated_at) VALUES(?,?,?)', (uid, int(m), now().isoformat(timespec='seconds')))
    conn.commit()
    return int(m)

def points_info(uid):
    r_all, cut_all = rows_for(uid, 'all')
    all_data = score(r_all, uid, 'all', cut_all)
    
    r_daily, cut_daily = rows_for(uid, 'daily')
    daily_res = score(r_daily, uid, 'daily', cut_daily)
    all_data['daily_points'] = daily_res['points']
    all_data['daily_accounts'] = daily_res['accounts']
    
    r_weekly, cut_weekly = rows_for(uid, 'weekly')
    weekly_res = score(r_weekly, uid, 'weekly', cut_weekly)
    all_data['weekly_points'] = weekly_res['points']
    all_data['weekly_accounts'] = weekly_res['accounts']
    
    all_data['record'] = lifetime_record(uid)
    all_data['pending'] = conn.execute("SELECT COUNT(*) FROM reports WHERE user_id=? AND status='pending'", (uid,)).fetchone()[0]
    all_data['badge'] = get_badge(all_data['points'])
    return all_data

def board_data(scope):
    ids = conn.execute("SELECT DISTINCT user_id FROM reports WHERE status='approved' UNION SELECT DISTINCT user_id FROM manual_adjustments").fetchall()
    result = []
    for r in ids:
        uid = r['user_id']
        rows, cut = rows_for(uid, scope)
        s = score(rows, uid, scope, cut)
        if s['points'] > 0 or s['accounts'] > 0:
            result.append((s['points'], s['accounts'], uid))
    return sorted(result, key=lambda x: (-x[0], -x[1], name_of(x[2]).lower()))

def board_text(scope):
    titles = {'daily': '🌟 DAILY LEADERBOARD 🌟', 'weekly': '🔥 WEEKLY LEADERBOARD 🔥', 'all': '👑 ALL-TIME HALL OF FAME 👑'}
    ranked = board_data(scope)
    
    if not ranked: return f"{titles[scope]}\n═══════════════════════\n⚠️ এখনো কোনো লিডারবোর্ড ডেটা পাওয়া যায়নি!"
    
    lines = [titles[scope], '═══════════════════════']
    
    top_king = ranked[0]
    lines.append(f"🏆 **TOP KING:** {name_of(top_king[2])}")
    lines.append(f"   💎 **Points:** `{top_king[0]}` | 🎯 **Accounts:** `{top_king[1]}`")
    lines.append("═══════════════════════")
    
    medals = ['🥇', '🥈', '🥉']
    for rank, (pts, accs, uid) in enumerate(ranked[:15], 1):
        mark = medals[rank - 1] if rank <= 3 else f'`#{rank}`'
        lines.append(f"{mark} **{name_of(uid)}**\n   └ ⚡ `{pts} pts` | 🎯 `{accs} acc`")
        
    lines.append('═══════════════════════')
    return '\n'.join(lines)

def review_kb(rep_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Approve', callback_data=f'review:approve:{rep_id}'),
         InlineKeyboardButton('❌ Reject', callback_data=f'review:reject:{rep_id}')]
    ])

def user_pending_kb(rep_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✏️ Edit Count', callback_data=f'useredit:{rep_id}'),
         InlineKeyboardButton('🗑️ Delete Report', callback_data=f'userdel:{rep_id}')]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_member(update.effective_user)
    await update.effective_message.reply_text(
        f"🌟 **Welcome to RoMi STAR BOARD, {update.effective_user.first_name}!** 🌟\n\n"
        f"🚀 আপনার কাজের সঠিক মূল্যায়ন এবং লিডারবোর্ডে টপ করার সেরা ঠিকানা!\n"
        f"👇 নিচের মেনু থেকে আপনার পছন্দসই অপশন বেছে নিন:",
        reply_markup=menu_for(update.effective_user.id)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 **RoMi Leaderboard – Rulebook & Guide**\n"
        "═══════════════════════\n"
        "✨ **Points Calculation:**\n"
        "• ১টি Valid Account = `+1 Point`\n"
        "• Daily Target (50+ Acc) = `+2 Bonus Points`\n"
        "• ৩/৫/৭ দিন Streak = `+3/+5/+7 Bonus Points`\n"
        "• Personal Record ভাঙলে = `+10 Bonus Points`\n\n"
        "📌 **Missing Accounts Tracking:**\n"
        "আপনার উইথড্রর মিসিং হিসাব জমা দিতে কমান্ড ব্যবহার করুন:\n"
        "`/addmissing [Withdraw_No] [Count]`\n"
        "📌 *উদাহরণ:* `/addmissing 1 15` (১ম উইথড্রতে ১৫টি মিসিং)\n\n"
        "🛠️ **Report Editing:**\n"
        "Admin অ্যাপ্রুভ করার আগে আপনি আপনার পেন্ডিং রিপোর্ট এডিট বা ডিলিট করতে পারবেন।"
    )
    await update.effective_message.reply_text(msg)

async def notice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ **ব্যবহারের সঠিক নিয়ম:**\n"
            "`/notice আপনার গুরুত্বপূর্ণ নোটিশ মেসেজটি এখানে লিখুন`"
        )
        return

    notice_text = " ".join(context.args)
    members = conn.execute("SELECT user_id FROM members").fetchall()
    if not members:
        await update.effective_message.reply_text("⚠️ বটে বর্তমানে কোনো মেম্বার রেজিস্টার্ড নেই।")
        return

    count = 0
    full_message = f"📢 **IMPORTANT NOTICE** 📢\n═══════════════════════\n{notice_text}\n═══════════════════════"
    for m in members:
        try:
            await context.bot.send_message(m['user_id'], full_message)
            count += 1
        except Exception: pass
    await update.effective_message.reply_text(f"🚀 সফলভাবে **{count}** জন মেম্বারের কাছে ব্রডকাস্ট করা হয়েছে!")

async def addmissing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_member(update.effective_user)
    if len(context.args) < 2:
        await update.effective_message.reply_text("⚠️ **সঠিক ফরম্যাট:** `/addmissing [Withdraw_No] [Count]`")
        return
    try:
        w_no = int(context.args[0]); count = int(context.args[1])
        if w_no <= 0 or count < 0: raise ValueError
    except ValueError:
        await update.effective_message.reply_text("❌ উইথড্র নম্বর এবং মিসিং অ্যাকাউন্টের সংখ্যা সঠিক ধনাত্মক সংখ্যা হতে হবে।")
        return

    uid = update.effective_user.id
    conn.execute(
        "INSERT INTO missing_accounts(user_id, withdraw_no, missing_count, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id, withdraw_no) DO UPDATE SET missing_count=excluded.missing_count, created_at=excluded.created_at",
        (uid, w_no, count, now().isoformat(timespec='seconds'))
    )
    conn.commit()
    await update.effective_message.reply_text(f"✅ সফলভাবে সেভ হয়েছে!\n📌 উইথড্র `#{w_no}` ➔ **{count}** টি মিসিং অ্যাকাউন্ট রেকর্ড করা হয়েছে।")

async def mymissing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    save_member(update.effective_user)
    rows = conn.execute("SELECT withdraw_no, missing_count FROM missing_accounts WHERE user_id=? ORDER BY withdraw_no", (uid,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("📭 আপনার কোনো মিসিং অ্যাকাউন্টের রেকর্ড পাওয়া যায়নি।")
        return
    lines = ["📌 **আপনার মিসিং অ্যাকাউন্টের তালিকা:**", "═══════════════════════"]
    total = 0
    for r in rows:
        lines.append(f"• উইথড্র `#{r['withdraw_no']}` ➔ **{r['missing_count']}** টি")
        total += int(r['missing_count'])
    lines.append("═══════════════════════")
    lines.append(f"🔥 **সর্বমোট মিসিং অ্যাকাউন্ট:** `{total} টি`")
    await update.effective_message.reply_text("\n".join(lines))

async def allmissing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = conn.execute("SELECT user_id, withdraw_no, missing_count FROM missing_accounts ORDER BY user_id, withdraw_no").fetchall()
    if not rows:
        await update.effective_message.reply_text("📭 কারো কোনো মিসিং অ্যাকাউন্টের হিসাব পাওয়া যায়নি।")
        return
    lines = ["📊 **সকল মেম্বারদের মিসিং অ্যাকাউন্টের হিসাব:**", "═══════════════════════"]
    current_uid = None; user_total = 0; grand_total = 0
    for r in rows:
        uid = r['user_id']
        if uid != current_uid:
            if current_uid is not None:
                lines.append(f"   └ **মোট মিসিং:** `{user_total} টি`\n")
            current_uid = uid; user_total = 0
            lines.append(f"👤 **User:** {name_of(uid)}")
        lines.append(f"   • Withdraw `#{r['withdraw_no']}` ➔ `{r['missing_count']} টি`")
        user_total += int(r['missing_count']); grand_total += int(r['missing_count'])
    if current_uid is not None:
        lines.append(f"   └ **মোট মিসিং:** `{user_total} টি`")
    lines.append("═══════════════════════")
    lines.append(f"🔥 **গ্রুপের সর্বমোট মিসিং অ্যাকাউন্ট:** `{grand_total} টি`")
    await update.effective_message.reply_text("\n".join(lines))

async def editmissing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 3:
        await update.effective_message.reply_text("⚠️ ব্যবহার: `/editmissing USER_ID WITHDRAW_NO NEW_COUNT`")
        return
    try:
        uid = int(context.args[0]); w_no = int(context.args[1]); count = int(context.args[2])
        conn.execute("INSERT INTO missing_accounts(user_id, withdraw_no, missing_count, created_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(user_id, withdraw_no) DO UPDATE SET missing_count=excluded.missing_count",
                     (uid, w_no, count, now().isoformat(timespec='seconds')))
        conn.commit()
        await update.effective_message.reply_text(f"✅ User `{uid}` এর উইথড্র `#{w_no}` এর মিসিং সংখ্যা সংশোধন করে `{count}` করা হয়েছে।")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")

async def delmissing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2:
        await update.effective_message.reply_text("⚠️ ব্যবহার: `/delmissing USER_ID WITHDRAW_NO`")
        return
    try:
        uid = int(context.args[0]); w_no = int(context.args[1])
        conn.execute("DELETE FROM missing_accounts WHERE user_id=? AND withdraw_no=?", (uid, w_no))
        conn.commit()
        await update.effective_message.reply_text(f"🗑️ User `{uid}` এর উইথড্র `#{w_no}` এর মিসিং রেকর্ড মুছে ফেলা হয়েছে।")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")

async def begin_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_report'] = True
    context.user_data['editing_rep_id'] = None
    await update.effective_message.reply_text('📝 **আজকের কাজের সংখ্যা লিখে বা স্ক্রিনশট প্রুফসহ সেন্ড করো।**')

async def handle_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw_text = (msg.caption or msg.text or '').strip()
    menu_buttons = ['Submit Report', 'My Points', 'My Missing Accounts', 'Leaderboard', 'Weekly Board', 'My Profile', 'Admin Panel', 'Help']
    if raw_text in menu_buttons or any(b.lower() in raw_text.lower() for b in menu_buttons):
        context.user_data['waiting_report'] = False
        context.user_data['editing_rep_id'] = None
        await menu_router(update, context)
        return

    if not context.user_data.get('waiting_report'):
        await menu_router(update, context)
        return

    photo_id = msg.photo[-1].file_id if msg.photo else ""
    try:
        n = int(raw_text)
        if n < 0 or n > 100000: raise ValueError
    except ValueError:
        await update.effective_message.reply_text('⚠️ সঠিক সংখ্যাটি লিখুন (০ থেকে ১০০০০০ এর মধ্যে)।')
        return
    
    user = update.effective_user; save_member(user); d = today()
    editing_id = context.user_data.get('editing_rep_id')

    if editing_id:
        row = conn.execute("SELECT status FROM reports WHERE id=? AND user_id=?", (editing_id, user.id)).fetchone()
        if row and row['status'] == 'pending':
            conn.execute("UPDATE reports SET count=?, photo_id=CASE WHEN ?!='' THEN ? ELSE photo_id END WHERE id=?", 
                         (n, photo_id, photo_id, editing_id))
            conn.commit()
            context.user_data['waiting_report'] = False; context.user_data['editing_rep_id'] = None
            await update.effective_message.reply_text(f"✏️ আপনার রিপোর্ট (`ID: #{editing_id}`) সংশোধন করে `{n}` করা হয়েছে।")
            for aid in ADMIN_IDS:
                try: await context.bot.send_message(aid, f"🔄 **REPORT EDITED BY USER**\n👤 User: {user.full_name}\n📌 Report ID: `#{editing_id}`\n🎯 New Count: `{n}`")
                except Exception: pass
            return

    cur = conn.execute("INSERT INTO reports(user_id,day,count,status,created_at,photo_id) VALUES(?,?,?,'pending',?,?)", 
                       (user.id, d, n, now().isoformat(timespec='seconds'), photo_id))
    conn.commit()
    rep_id = cur.lastrowid
    context.user_data['waiting_report'] = False
    
    await update.effective_message.reply_text(
        f'🚀 **Report Submitted Successfully!**\n📅 Date: `{d}`\n🎯 Accounts: `{n}`\n⏳ Admin review-এর অপেক্ষায় রয়েছে।', 
        reply_markup=user_pending_kb(rep_id)
    )
    
    for aid in ADMIN_IDS:
        try:
            admin_msg = f'📥 **New Pending Report (`#{rep_id}`)**\n👤 User: {user.full_name}\n🆔 ID: `{user.id}`\n📅 Date: `{d}`\n🎯 Accounts: `{n}`'
            if photo_id: await context.bot.send_photo(aid, photo=photo_id, caption=admin_msg, reply_markup=review_kb(rep_id))
            else: await context.bot.send_message(aid, text=admin_msg, reply_markup=review_kb(rep_id))
        except Exception: log.exception('Admin notify failed')

async def user_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data.split(':'); action = data[0]; rep_id = int(data[1]); uid = q.from_user.id
    row = conn.execute("SELECT * FROM reports WHERE id=? AND user_id=?", (rep_id, uid)).fetchone()
    if not row or row['status'] != 'pending':
        await q.answer("⚠️ রিপোর্টটি পাওয়া যায়নি বা ইতিমধ্যে রিভিউ হয়ে গেছে!", show_alert=True); return

    if action == 'userdel':
        conn.execute("DELETE FROM reports WHERE id=?", (rep_id,))
        conn.commit()
        await q.edit_message_text("🗑️ আপনার রিপোর্টটি সফলভাবে ডিলিট করা হয়েছে।")
    elif action == 'useredit':
        context.user_data['waiting_report'] = True; context.user_data['editing_rep_id'] = rep_id
        await q.edit_message_text("✏️ আপনার সঠিক কাজের সংখ্যাটি লিখে বা ছবিসহ আবার পাঠান:")

async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; save_member(u); x = points_info(u.id)
    await update.effective_message.reply_text(
        f"💎 **MY STATS & POINTS**\n"
        f"═══════════════════════\n"
        f"👤 **User:** {name_of(u.id)}\n"
        f"🏅 **Badge:** `{x['badge']}`\n"
        f"✨ **Total Points:** `{x['points']} pts`\n"
        f"🎯 **Total Accounts:** `{x['accounts']} acc`\n"
        f"═══════════════════════\n"
        f"📅 **Daily:** `{x['daily_points']} pts` (`{x['daily_accounts']} acc`)\n"
        f"📊 **Weekly:** `{x['weekly_points']} pts` (`{x['weekly_accounts']} acc`)\n"
        f"🏆 **Best Day Record:** `{x['record']} acc`\n"
        f"⏳ **Pending Reports:** `{x['pending']}`"
    )

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; save_member(u); x = points_info(u.id); ranked = board_data('all')
    rank = next((i for i, (_, _, uid) in enumerate(ranked, 1) if uid == u.id), None)
    await update.effective_message.reply_text(
        f"👤 **MY PROFILE CARD**\n"
        f"═══════════════════════\n"
        f"📌 **Name:** {u.full_name}\n"
        f"🆔 **User ID:** `{u.id}`\n"
        f"🏆 **Global Rank:** `#{rank if rank else 'Unranked'}`\n"
        f"🏅 **Badge:** `{x['badge']}`\n\n"
        f"✨ **Total Points:** `{x['points']} pts`\n"
        f"🎯 **Total Accounts:** `{x['accounts']} acc`\n"
        f"🔥 **Best Record:** `{x['record']} acc`\n"
        f"⏳ **Pending:** `{x['pending']}`"
    )

async def show_board(update, context, scope): await update.effective_message.reply_text(board_text(scope))
async def daily_board(update, context): await show_board(update, context, 'daily')
async def weekly_board(update, context): await show_board(update, context, 'weekly')
async def total_board(update, context): await show_board(update, context, 'all')

async def clear_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn.execute("DELETE FROM reports"); conn.execute("DELETE FROM personal_records")
    conn.execute("DELETE FROM manual_adjustments"); conn.execute("DELETE FROM reset_log"); conn.execute("DELETE FROM missing_accounts")
    conn.commit()
    await update.effective_message.reply_text("🔄 ডাটাবেজ সফলভাবে সম্পূর্ণ রিসেট করা হয়েছে!")

async def announce_mvp_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): await q.answer('⚠️ শুধু Admin এটি করতে পারবেন।', show_alert=True); return
    d = today()
    rows = conn.execute("SELECT user_id, SUM(count) as total_cnt FROM reports WHERE day=? AND status='approved' GROUP BY user_id ORDER BY total_cnt DESC LIMIT 3", (d,)).fetchall()
    if not rows:
        await q.answer("⚠️ আজকের কোনো অনুমোদিত রিপোর্ট পাওয়া যায়নি!", show_alert=True); return
    lines = ["🌟 **DAILY MVP / TOP 3 CHAMPIONS** 🌟", "═══════════════════════", f"📅 Date: `{d}`\n"]
    medals = ['🥇', '🥈', '🥉']
    for idx, r in enumerate(rows):
        uid = r['user_id']; cnt = r['total_cnt']; pts = points_info(uid)['points']
        lines.append(f"{medals[idx]} **Top #{idx+1}: {name_of(uid)}**\n   └ 🎯 `{cnt} Accounts` | ✨ `{pts} pts`")
    lines.append("\n🎉 অসাধারণ পারফরম্যান্সের জন্য সবাইকে অভিনন্দন!")
    mvp_card = "\n".join(lines)
    for m in conn.execute("SELECT user_id FROM members").fetchall():
        try: await context.bot.send_message(m['user_id'], mvp_card)
        except Exception: pass
    await q.edit_message_text("✅ আজকের Top 3 Daily MVP সফলভাবে ব্রডকাস্ট করা হয়েছে!")

async def send_reminder_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    count = 0
    for m in conn.execute("SELECT user_id FROM members").fetchall():
        try: await context.bot.send_message(m['user_id'], "⏰ **DAILY WORK REPORT REMINDER**\n⚡ দ্রুত আপনার আজকের কাজের সংখ্যা জমা দিন!"); count += 1
        except Exception: pass
    await q.edit_message_text(f"✅ রিমাইন্ডার সফলভাবে **{count}** জন মেম্বারের কাছে পাঠানো হয়েছে।")

async def cycle_summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = conn.execute("SELECT user_id, SUM(count) as c FROM reports WHERE status='approved' GROUP BY user_id ORDER BY c DESC LIMIT 5").fetchall()
    if not rows: await update.effective_message.reply_text("📭 কোনো অনুমোদিত রিপোর্ট নেই।"); return
    lines = ["📊 **WORK CYCLE SUMMARY (Top 5)**", "═══════════════════════"]
    for i, r in enumerate(rows, 1): lines.append(f"{i}. **{name_of(r['user_id'])}** ➔ `{r['c']} Accounts`")
    await update.effective_message.reply_text("\n".join(lines))

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text('⚠️ এই অপশন শুধু Admin-এর জন্য।'); return
    kb = InlineKeyboardMarkup([
      [InlineKeyboardButton('📢 Announce Top 3 MVP', callback_data='announce_mvp')],
      [InlineKeyboardButton('⏰ Send Reminder', callback_data='send_reminder')],
      [InlineKeyboardButton('🔄 Reset Daily', callback_data='reset:daily'), InlineKeyboardButton('📊 Reset Weekly', callback_data='reset:weekly')],
      [InlineKeyboardButton('⚠️ Reset All', callback_data='reset:all'), InlineKeyboardButton('⏳ Pending Reports', callback_data='reset:pending')]
    ])
    mult = get_multiplier()
    await update.effective_message.reply_text(
        f'⚡ **ADMIN CONTROL PANEL** ⚡\n'
        f'═══════════════════════\n'
        f'⚙️ **Current Multiplier:** `{mult}x`\n\n'
        f'🛠️ **Admin Commands:**\n'
        f'• নোটিশ: `/notice [Msg]` | সামারি: `/summary`\n'
        f'• পয়েন্ট যোগ: `/addpts UID PTS` | কাটুন: `/deductpts UID PTS`\n'
        f'• মিসিং লিস্ট: `/allmissing`\n\n'
        '👇 নিচের অপশনগুলো থেকে বেছে নিন:', reply_markup=kb
    )

async def happyhour_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: return
    try: val = float(context.args[0])
    except ValueError: return
    conn.execute("INSERT INTO settings(key, value) VALUES('multiplier', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(val),))
    conn.commit()
    await update.effective_message.reply_text(f"🔥 **Happy Hour Activated!** Multiplier set to `{val}x`")

async def addpts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) < 2: return
    try:
        uid = int(context.args[0]); pts = int(context.args[1]); reason = " ".join(context.args[2:]) or "Bonus"
        conn.execute("INSERT INTO manual_adjustments(user_id, points, reason, created_at) VALUES(?,?,?,?)", (uid, pts, reason, now().isoformat(timespec='seconds')))
        conn.commit()
        await update.effective_message.reply_text(f"✅ User `{uid}`-কে `+{pts}` পয়েন্ট যোগ করা হয়েছে।")
        try: await context.bot.send_message(uid, f"🎉 অভিনন্দন! Admin আপনার অ্যাকাউন্টে `+{pts}` বোনাস পয়েন্ট যোগ করেছেন!")
        except Exception: pass
    except Exception as e: await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")

async def deductpts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) < 2: return
    try:
        uid = int(context.args[0]); pts = int(context.args[1]); reason = " ".join(context.args[2:]) or "Penalty"
        conn.execute("INSERT INTO manual_adjustments(user_id, points, reason, created_at) VALUES(?,?,?,?)", (uid, -pts, reason, now().isoformat(timespec='seconds')))
        conn.commit()
        await update.effective_message.reply_text(f"⚠️ User `{uid}`-এর অ্যাকাউন্ট থেকে `-{pts}` পয়েন্ট কমানো হয়েছে।")
        try: await context.bot.send_message(uid, f"⚠️ Admin আপনার অ্যাকাউন্ট থেকে `-{pts}` পয়েন্ট সমন্বয় করেছেন।")
        except Exception: pass
    except Exception as e: await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")

async def setrecord_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 2: return
    uid = int(context.args[0]); count = int(context.args[1])
    conn.execute('INSERT INTO personal_records(user_id, best_count, updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET best_count=excluded.best_count', (uid, count, now().isoformat(timespec='seconds')))
    conn.commit()
    await update.effective_message.reply_text(f"✅ User `{uid}`-এর Record `{count}` সেট করা হয়েছে।")

async def deletereport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: return
    rep_id = int(context.args[0])
    row = conn.execute("SELECT * FROM reports WHERE id=?", (rep_id,)).fetchone()
    if not row: await update.effective_message.reply_text("⚠️ রিপোর্ট পাওয়া যায়নি।"); return
    uid = row['user_id']
    conn.execute("DELETE FROM reports WHERE id=?", (rep_id,)); conn.commit()
    current_max = conn.execute("SELECT MAX(count) as m FROM reports WHERE user_id=? AND status='approved'", (uid,)).fetchone()['m'] or 0
    conn.execute('INSERT INTO personal_records(user_id, best_count, updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET best_count=excluded.best_count', (uid, int(current_max), now().isoformat(timespec='seconds')))
    conn.commit()
    await update.effective_message.reply_text(f"🗑️ Report `#{rep_id}` ডিলিট করা হয়েছে।")

async def reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    action = q.data.split(':', 1)[1]
    if action == 'pending': await q.answer(); await send_pending(q, context); return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Confirm', callback_data=f'resetconfirm:{action}'),
        InlineKeyboardButton('❌ Cancel', callback_data='resetcancel')
    ]])
    await q.edit_message_text(f'⚠️ নিশ্চিত করুন: `{action.capitalize()}` ডাটা রিসেট হবে।', reply_markup=kb)

async def reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    action = q.data.split(':', 1)[1]; stamp = now().isoformat(timespec='seconds')
    scope, day = (action, today() if action == 'daily' else '')
    conn.execute('INSERT INTO reset_log(scope, target_day, cutoff_at, admin_id) VALUES(?,?,?,?)', (scope, day, stamp, q.from_user.id))
    conn.commit()
    await q.edit_message_text(f"✅ `{action.capitalize()}` Reset সফল হয়েছে।")

async def cancel_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text('❌ বাতিল করা হয়েছে।')

async def send_pending(target, context):
    rows = conn.execute("SELECT id,user_id,day,count,photo_id FROM reports WHERE status='pending' ORDER BY id").fetchall()
    if not rows: await target.message.reply_text('📭 কোনো pending report নেই।'); return
    await target.message.reply_text(f'⏳ **Pending Reports:** `{len(rows)}`')
    for r in rows:
        text = f"📥 **Pending Report (`#{r['id']}`)**\n👤 User: {name_of(r['user_id'])}\n🎯 Accounts: `{r['count']}`"
        if r['photo_id']: await target.message.reply_photo(photo=r['photo_id'], caption=text, reply_markup=review_kb(r['id']))
        else: await target.message.reply_text(text, reply_markup=review_kb(r['id']))

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id): await send_pending(update.effective_message, context)

async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    try: _, action, rep_id = q.data.split(':'); rep_id = int(rep_id)
    except Exception: return
    row_data = conn.execute('SELECT * FROM reports WHERE id=? AND status=\'pending\'', (rep_id,)).fetchone()
    if not row_data: await q.edit_message_text('⚠️ ইতিমধ্যে প্রসেস করা হয়েছে।'); return
    uid = row_data['user_id']; d = row_data['day']; n_report = int(row_data['count'])
    old_record = lifetime_record(uid); status = 'approved' if action == 'approve' else 'rejected'
    new_record = (action == 'approve' and n_report > old_record)
    conn.execute("UPDATE reports SET status=?, reviewed_at=?, record_bonus=? WHERE id=?", 
                 (status, now().isoformat(timespec='seconds'), (10 if new_record else 0), rep_id))
    if new_record:
        conn.execute('INSERT INTO personal_records(user_id, best_count, updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET best_count=excluded.best_count', (uid, n_report, now().isoformat(timespec='seconds')))
    conn.commit()
    if action == 'approve':
        info = points_info(uid); res = f"✅ **Report #{rep_id} Approved!**"
        try: await context.bot.send_message(uid, f"🎉 অভিনন্দন! আপনার রিপোর্ট (`ID: #{rep_id}`) **Approved** হয়েছে! ✨ Points: `{info['points']}`")
        except Exception: pass
    else:
        res = f'❌ **Report #{rep_id} Rejected.**'
        try: await context.bot.send_message(uid, f'❌ দুঃখিত! আপনার রিপোর্ট (`ID: #{rep_id}`) **Reject** হয়েছে।')
        except Exception: pass
    if q.message.photo: await q.edit_message_caption(caption=res)
    else: await q.edit_message_text(res)

async def editreport_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) == 2:
        try:
            rep_id = int(context.args[0]); new_count = int(context.args[1])
            conn.execute("UPDATE reports SET count=? WHERE id=?", (new_count, rep_id))
            conn.commit()
            await update.effective_message.reply_text(f"✅ Report `#{rep_id}` আপডেট করা হয়েছে।")
        except Exception as e: await update.effective_message.reply_text(f"❌ ত্রুটি: {e}")

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or '').strip(); save_member(update.effective_user)
    routes = {
        'Submit Report': begin_report, 'My Points': my_points, 'My Missing Accounts': mymissing_cmd,
        'Leaderboard': total_board, 'Weekly Board': weekly_board, 'My Profile': my_profile,
        'Admin Panel': admin_panel, 'Help': help_cmd
    }
    for k, fn in routes.items():
        if k.lower() in text.lower(): await fn(update, context); return
    await update.effective_message.reply_text('👇 অপশন বেছে নিন:', reply_markup=menu_for(update.effective_user.id))

async def error_handler(update, context): log.exception('Unhandled bot error', exc_info=context.error)

def main():
    if not TOKEN: raise SystemExit('❌ BOT_TOKEN সেট করা নেই।')
    if not ADMIN_IDS: raise SystemExit('❌ ADMIN_IDS সেট করা নেই।')
    
    # Start the Flask Keep-Alive Server thread
    keep_alive()

    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('pending', pending_cmd))
    app.add_handler(CommandHandler('admin', admin_panel))
    app.add_handler(CommandHandler('editreport', editreport_cmd))
    app.add_handler(CommandHandler('deletereport', deletereport_cmd))
    app.add_handler(CommandHandler('addpts', addpts_cmd))
    app.add_handler(CommandHandler('deductpts', deductpts_cmd))
    app.add_handler(CommandHandler('happyhour', happyhour_cmd))
    app.add_handler(CommandHandler('setrecord', setrecord_cmd))
    app.add_handler(CommandHandler('summary', cycle_summary_cmd))
    app.add_handler(CommandHandler('clearhistory', clear_history_cmd))
    app.add_handler(CommandHandler('notice', notice_cmd))
    
    app.add_handler(CommandHandler('addmissing', addmissing_cmd))
    app.add_handler(CommandHandler('mymissing', mymissing_cmd))
    app.add_handler(CommandHandler('allmissing', allmissing_cmd))
    app.add_handler(CommandHandler('editmissing', editmissing_cmd))
    app.add_handler(CommandHandler('delmissing', delmissing_cmd))
    
    app.add_handler(CallbackQueryHandler(review_callback, pattern=r'^review:'))
    app.add_handler(CallbackQueryHandler(announce_mvp_cb, pattern=r'^announce_mvp$'))
    app.add_handler(CallbackQueryHandler(send_reminder_cb, pattern=r'^send_reminder$'))
    app.add_handler(CallbackQueryHandler(user_action_callback, pattern=r'^(useredit|userdel):'))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern=r'^reset:'))
    app.add_handler(CallbackQueryHandler(reset_confirm, pattern=r'^resetconfirm:'))
    app.add_handler(CallbackQueryHandler(cancel_reset, pattern=r'^resetcancel$'))
    
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_submission))
    app.add_error_handler(error_handler)
    
    log.info('🚀 RoMi STAR BOARD Master Edition (24/7 Web Service) is starting...')
    app.run_polling(drop_pending_updates=False)

if __name__=='__main__': main()