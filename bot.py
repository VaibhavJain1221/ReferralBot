import logging
import telegram.error
from telegram import ReplyKeyboardMarkup, KeyboardButton
import random
import telegram
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import secrets
import string
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import html
from flask import Flask
import threading, os

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
MONGODB_URL = os.getenv("MONGODB_URL")
OWNER_ID = int(os.getenv("OWNER_ID"))
REQUIRED_CHANNELS = [
    {"id": "@FreeNetflixDaily0", "name": "Free Netflix Daily!"},
    {"id": "@FreeNetflixDailyChat", "name": "Free Netflix Daily Chat!"}
]

pending_referrals = {}  # user_id -> referred_by

# MongoDB configuration
 # Change to MongoDB Atlas URL when ready
DATABASE_NAME = os.getenv("DATABASE_NAME")


app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running ✅"


# Global variables to store context for message handling
current_waiting_for_code = set()
current_waiting_for_custom_code = set()
current_waiting_for_withdraw_files = set()
current_waiting_for_claim_files = set()
current_waiting_for_code_users = set()
pending_code_data = {}  # Store temporary data for code generation


# Webhook route
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    asyncio.run(application.process_update(update))
    return "OK"

# MongoDB connection
def get_db():
    client = MongoClient(MONGODB_URL)
    return client[DATABASE_NAME]

# Database initialization
def init_db():
    db = get_db()
    
    # TTL index: 15 days for withdraw and claim logs
    db.withdraw_logs.create_index("timestamp", expireAfterSeconds=1296000)
    db.claim_logs.create_index("timestamp", expireAfterSeconds=1296000)

    # TTL index: 30 days for code claims (optional)
    db.code_claims.create_index("claimed_at", expireAfterSeconds=2592000)

    # Main collections
    users_collection = db.users
    claim_codes_collection = db.claim_codes
    code_claims_collection = db.code_claims
    bot_settings_collection = db.bot_settings
    files_collection = db.files

    # Indexes
    try:
        users_collection.create_index("user_id", unique=True)
    except:
        pass

    try:
        claim_codes_collection.create_index("code", unique=True)
    except:
        pass

    # ✅ ✅ FIXED: Use code_id instead of code to avoid reuse conflict
    try:
        code_claims_collection.drop_index([("user_id", 1), ("code", 1)])  # Drop incorrect index if exists
    except:
        pass  # Safe if index doesn't exist

    try:
        code_claims_collection.create_index([("user_id", 1), ("code_id", 1)], unique=True)  # ✅ Correct index
    except:
        pass

    try:
        bot_settings_collection.create_index("key", unique=True)
    except:
        pass

    try:
        files_collection.create_index("file_id")
    except:
        pass

    # Set default bot settings
    default_settings = [
        {"key": "withdraw_files", "value": 0},
        {"key": "claim_files", "value": 0}
    ]

    for setting in default_settings:
        if not bot_settings_collection.find_one({"key": setting["key"]}):
            try:
                bot_settings_collection.insert_one(setting)
                logger.info(f"Initialized setting: {setting['key']} = {setting['value']}")
            except DuplicateKeyError:
                pass

    logger.info("Database initialized successfully")


def store_file_info(file_id, file_type, file_name, user_id):
    """Store file information in database"""
    db = get_db()
    file_data = {
        "file_id": file_id,
        "file_type": file_type,  # 'withdraw' or 'claim'
        "file_name": file_name,
        "uploaded_by": user_id,
        "uploaded_at": datetime.now()
    }
    db.files.insert_one(file_data)

# Database helper functions
def get_user(user_id):
    db = get_db()
    return db.users.find_one({"user_id": user_id})

def add_user(user_id, username, first_name, referred_by=None):
    db = get_db()
    
    # Check if user already exists
    existing_user = get_user(user_id)
    if existing_user:
        return False
    
    user_data = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "points": 0,
        "referred_by": referred_by,
        "last_withdrawal": None,
        "join_date": datetime.now(),
        "is_referred": bool(referred_by)
    }
    
    try:
        db.users.insert_one(user_data)
        return True
    except DuplicateKeyError:
        return False

def update_user_points(user_id, points):
    db = get_db()
    db.users.update_one(
        {"user_id": user_id},
        {"$inc": {"points": points}}
    )

def can_withdraw(user_id):
    user = get_user(user_id)
    if not user or not user.get("last_withdrawal"):
        return True
    
    last_withdrawal = user["last_withdrawal"]
    return datetime.now() - last_withdrawal >= timedelta(hours=4)

def update_withdrawal_time(user_id):
    db = get_db()
    db.users.update_one(
        {"user_id": user_id},
        {"$set": {"last_withdrawal": datetime.now()}}
    )

def get_bot_settings():
    db = get_db()
    settings = {}
    for setting in db.bot_settings.find():
        settings[setting["key"]] = setting["value"]
    return settings

def update_bot_setting(key, value):
    db = get_db()
    db.bot_settings.update_one(
        {"key": key},
        {"$set": {"value": value}},
        upsert=True
    )

def create_claim_code(code, files_count, created_by):
    db = get_db()
    code_data = {
        "code": code,
        "files_left": files_count,
        "created_by": created_by,
        "created_at": datetime.now(),
        "is_active": True
    }
    
    try:
        db.claim_codes.insert_one(code_data)
        return True
    except DuplicateKeyError:
        return False

def get_claim_code(code):
    db = get_db()
    return db.claim_codes.find_one({"code": code, "is_active": True})

from bson.objectid import ObjectId
from pymongo.errors import DuplicateKeyError
from datetime import datetime

def use_claim_code(user_id, code):
    db = get_db()

    # Get active claim code info
    claim_code = db.claim_codes.find_one({
        "code": code,
        "is_active": True
    })
    if not claim_code:
        return False, "invalid_code"

    # Check if user already claimed this exact code (by ID)
    existing_claim = db.code_claims.find_one({
        "user_id": user_id,
        "code_id": claim_code["_id"]
    })
    if existing_claim:
        return False, "already_claimed"

    if claim_code["files_left"] <= 0:
        # ❌ Code exhausted — delete it
        db.claim_codes.delete_one({"_id": claim_code["_id"]})
        return False, "no_files"

    # Record the claim with code_id
    try:
        db.code_claims.insert_one({
            "user_id": user_id,
            "code_id": claim_code["_id"],
            "code": code,
            "claimed_at": datetime.now()
        })
    except DuplicateKeyError:
        return False, "already_claimed"

    # Decrease files_left
    db.claim_codes.update_one(
        {"_id": claim_code["_id"]},
        {"$inc": {"files_left": -1}}
    )

    # Update global stats
    settings = get_bot_settings()
    update_bot_setting('claim_files', max(0, settings.get('claim_files', 0) - 1))

    # If code is now empty, delete it
    updated_code = db.claim_codes.find_one({"_id": claim_code["_id"]})
    if updated_code and updated_code["files_left"] <= 0:
        db.claim_codes.delete_one({"_id": updated_code["_id"]})

    return True, updated_code["files_left"] if updated_code else 0


def get_random_claim_file():
    """Get a random file for claim code redemption"""
    db = get_db()
    # Get a random file from claim files
    files = list(db.files.find({"file_type": "claim_files"}))
    if files:
        return random.choice(files)
    return None

def get_random_withdraw_file():
    """Get a random file for withdrawal"""
    db = get_db()
    # Get a random file from withdraw files
    files = list(db.files.find({"file_type": "withdraw_files"}))
    if files:
        return random.choice(files)
    return None

def log_withdrawal(user_id, username, file_id, file_name):
    from datetime import datetime
    db = get_db()  
    db["withdraw_logs"].insert_one({
        "user_id": user_id,
        "username": username,
        "file_id": file_id,
        "file_name": file_name,
        "timestamp": datetime.now()
    })
def log_claim(user_id, username, file_id, file_name, claim_code):
    from datetime import datetime
    db = get_db()
    db["claim_logs"].insert_one({
        "user_id": user_id,
        "username": username,
        "file_id": file_id,
        "file_name": file_name,
        "claim_code": claim_code,
        "timestamp": datetime.now()
    })
def delete_claim_file(file_id):
    db = get_db()
    result = db.files.delete_one({
        "_id": file_id,
        "file_type": "claim_files"
    })
    logger.info(f"Deleted claim file: {file_id} → matched: {result.deleted_count}")


def delete_withdraw_file(file_id):
    db = get_db()
    result = db.files.delete_one({
        "_id": file_id,
        "file_type": "withdraw_files"
    })
    logger.info(f"Deleted file: {file_id} → matched: {result.deleted_count}")


# Check if user is member of required channels
async def check_channel_membership(bot: Bot, user_id: int) -> bool:
    # ✅ Skip check for owner/admin (optional, safe)
    if user_id == OWNER_ID:
        return True

    try:
        for channel in REQUIRED_CHANNELS:
            try:
                member = await bot.get_chat_member(channel["id"], user_id)
                if member.status not in ("member", "administrator", "creator"):
                    logger.warning(f"User {user_id} is not a member of {channel['id']} (status: {member.status})")
                    return False
            except Exception as e:
                logger.error(f"Error checking {channel['id']} for user {user_id}: {e}")
                return False  # Fail-safe: treat as not joined
        return True
    except Exception as e:
        logger.error(f"General error checking membership for user {user_id}: {e}")
        return False


# Generate claim code
def generate_claim_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

async def verify_membership(bot, user_id):
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(channel["id"], user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except:
            return False
    return True

# Start command handler
# Check if user already exists
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    if update.message.chat.type != "private":
        return

    referred_by = None
    if args and args[0].startswith('ref_'):
        try:
            referred_by = int(args[0][4:])
        except ValueError:
            referred_by = None

    # If user hasn't joined required channels, prompt and save referral
    if not await check_channel_membership(context.bot, user.id):
        if referred_by:
            pending_referrals[user.id] = referred_by  # ✅ Save referral temporarily

        keyboard = []
        for channel in REQUIRED_CHANNELS:
            keyboard.append([InlineKeyboardButton(f"Join {channel['name']}", url=f"https://t.me/{channel['id'][1:]}")])
        keyboard.append([InlineKeyboardButton("✅ I Joined", callback_data="check_membership")])

        await update.message.reply_text(
            "❌ You must join our channels to continue using the bot:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Check if user already exists
    existing_user = get_user(user.id)
    if existing_user:
        await show_main_menu(update, context)
        return

    # New user, apply referral (if exists)
    add_user(user.id, user.username, user.first_name, referred_by)

    if referred_by and referred_by != user.id:
        update_user_points(referred_by, 8)
        update_user_points(user.id, 4)

        await update.message.reply_text("🎉 You've earned 4 points for joining through a referral link!")

        try:
            await context.bot.send_message(
                referred_by,
                f"🎉 You earned 8 points! {user.first_name} joined using your referral link!"
            )
        except:
            pass

    await show_main_menu(update, context)




async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await start(update, context)
        return
    
    keyboard = [
        [InlineKeyboardButton("👤 My Profile", callback_data="my_profile")],
        [InlineKeyboardButton("⚡ Withdraw Points", callback_data="withdraw_points")],
        [InlineKeyboardButton("🎁 Claim Code", callback_data="claim_code")],
        [InlineKeyboardButton(
"📊 Stats", callback_data="stats")]
    ]
    
    # Add owner-only buttons
    if update.effective_user.id == OWNER_ID:
        keyboard.append([InlineKeyboardButton("🔐 Generate Code (Owner)", callback_data="generate_code")])
        keyboard.append([InlineKeyboardButton("📁 Add Files (Owner)", callback_data="add_files")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_text = f"🏠 Main Menu\n\nWelcome back, {update.effective_user.first_name}!"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup)

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error

    
    user = get_user(query.from_user.id)
    if not user:
        await query.edit_message_text("❌ User not found!")
        return
    
    referral_link = f"https://t.me/{context.bot.username}?start=ref_{user['user_id']}"
    
    profile_text = f"""
👤 **My Profile**

🆔 **User ID:** {user['user_id']}
👤 **Name:** {user['first_name']}
🎯 **Points:** {user['points']}
🔗 **Your Referral Link:**
`{referral_link}`

💡 Share your referral link to earn 8 points per referral!
New users get 4 points when they join through your link!
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(profile_text, reply_markup=reply_markup, parse_mode='Markdown')

async def withdraw_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error

    user_id = query.from_user.id
    user = get_user(user_id)
    if not user:
        await query.edit_message_text("❌ <b>User not found!</b>", parse_mode='HTML')
        return

    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # ✅ Channel check
    for channel in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(channel["id"], user_id)
            if member.status not in ("member", "administrator", "creator"):
                raise Exception("Not a member")
        except:
            join_keyboard = [
                [InlineKeyboardButton(f"Join {c['name']}", url=f"https://t.me/{c['id'][1:]}")] for c in REQUIRED_CHANNELS
            ]
            join_keyboard.append([InlineKeyboardButton("✅ I Joined", callback_data="check_membership")])
            await query.edit_message_text(
                "❌ <b>You must join our channels to use this feature.</b>",
                reply_markup=InlineKeyboardMarkup(join_keyboard),
                parse_mode='HTML'
            )
            return

    # ✅ Point check
    if user['points'] < 16:
        await query.edit_message_text(
            f"❌ <b>Not enough points!</b>\n\nYou have: <b>{user['points']} points</b>\nMinimum required: <b>16 points</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return

    # ✅ Cooldown check
    if not can_withdraw(user_id):
        last = user.get('last_withdrawal')
        if last:
            next_time = last + timedelta(hours=4)
            rem = next_time - datetime.now()
            h, r = divmod(rem.total_seconds(), 3600)
            m = r // 60
            await query.edit_message_text(
                f"⏰ <b>Cooldown Active</b>\n\nNext withdrawal: <b>{int(h)}h {int(m)}m</b>",
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            return

    # ✅ File availability
    settings = get_bot_settings()
    if settings.get('withdraw_files', 0) <= 0:
        await query.edit_message_text(
            "❌ <b>No files available for withdrawal!</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return

    update_user_points(user_id, -16)
    update_withdrawal_time(user_id)
    update_bot_setting('withdraw_files', settings['withdraw_files'] - 1)

    file_info = get_random_withdraw_file()
    if not file_info:
        await query.edit_message_text(
            "❌ <b>No files found!</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return

    safe_file_name = html.escape(file_info['file_name'])

    # ✅ Send the file
    try:
        await context.bot.send_document(
            chat_id=user_id,
            document=file_info['file_id'],
            caption=""  # no caption to avoid HTML parsing error
        )
    except Exception as e:
        logger.error(f"Error sending withdrawal file: {e}")
        await query.edit_message_text(
            (
                f"<b>✅ Points Deducted</b>\n"
                f"🎯 Remaining: <b>{user['points'] - 16}</b>\n\n"
                f"❌ <b>Error sending file.</b> Please contact admin.\n"
            ),
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
        return

    # ✅ Log and delete file
    log_withdrawal(
        user_id=user_id,
        username=user.get('username', 'N/A'),
        file_id=file_info['file_id'],
        file_name=file_info['file_name']
    )
    delete_withdraw_file(file_info["_id"])

    # ✅ Confirmation message (NOW FIXED)
    await query.edit_message_text(
        (
            f"<b>✅ Withdrawal Successful!</b>\n\n"
            f"💰 16 points deducted\n"
            f"🎯 Remaining points: <b>{user['points'] - 16}</b>\n"
            f"📁 File: {safe_file_name}\n\n"
            f"⏰ Next withdrawal available in 4 hours."
        ),
        reply_markup=reply_markup,
        parse_mode='HTML'
    )





async def claim_code_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error

    
    # Add user to waiting list
    current_waiting_for_code.add(query.from_user.id)
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎁 **Claim Code**\n\nSend me your claim code to redeem a file!\n\n💡 You can only use each code once.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error

    
    settings = get_bot_settings()
    
    # Get additional stats from MongoDB
    db = get_db()
    total_users = db.users.count_documents({})
    total_active_codes = db.claim_codes.count_documents({"is_active": True})
    
    stats_text = f"""
📊 **Bot Statistics**

👥 **Total Users:** {total_users}
📁 **Available Files for Withdrawal:** {settings.get('withdraw_files', 0)}
🎁 **Available Files for Claim Codes:** {settings.get('claim_files', 0)}
🔐 **Active Claim Codes:** {total_active_codes}
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(stats_text, reply_markup=reply_markup, parse_mode='Markdown')

async def generate_code_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error

    
    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ Unauthorized access!")
        return
    
    # Add owner to waiting list for number of users
    current_waiting_for_code_users.add(query.from_user.id)
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🔐 **Generate Custom Claim Code**\n\nStep 1: How many users can claim this code?\n\nSend me a number (e.g., 5, 10, 100):",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def add_files_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error

    
    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ Unauthorized access!")
        return
    
    keyboard = [
        [InlineKeyboardButton("📁 Add Withdrawal Files", callback_data="add_withdraw_files")],
        [InlineKeyboardButton("🎁 Add Claim Files", callback_data="add_claim_files")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    settings = get_bot_settings()
    
    await query.edit_message_text(
        f"📁 **File Management**\n\nCurrent Status:\n📤 Withdrawal Files: {settings.get('withdraw_files', 0)}\n🎁 Claim Files: {settings.get('claim_files', 0)}\n\nChoose an option:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def add_files(update: Update, context: ContextTypes.DEFAULT_TYPE, file_type: str):
    query = update.callback_query
    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Ignore "query is too old" error


    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ Unauthorized access!")
        return

    # Make sure user is ONLY in one upload mode
    current_waiting_for_withdraw_files.discard(query.from_user.id)
    current_waiting_for_claim_files.discard(query.from_user.id)

    if file_type == "withdraw_files":
        current_waiting_for_withdraw_files.add(query.from_user.id)
        file_type_name = "withdrawal"
    else:  # file_type == "claim_files"
        current_waiting_for_claim_files.add(query.from_user.id)
        file_type_name = "claim code"

    keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📁 **Add {file_type_name.title()} Files**\n\nPlease send the files you want to add for {file_type_name}.\n\n💡 You can send multiple files at once or send them one by one.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_claim_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()

    # ✅ Check if user is still in all required channels/groups
    for channel in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(channel["id"], user_id)
            if member.status not in ("member", "administrator", "creator"):
                raise Exception("Not a member")
        except:
            keyboard = [
                [InlineKeyboardButton(f"Join {c['name']}", url=f"https://t.me/{c['id'][1:]}")] for c in REQUIRED_CHANNELS
            ]
            keyboard.append([InlineKeyboardButton("✅ I Joined", callback_data="check_membership")])
            await update.message.reply_text(
                "❌ <b>You must rejoin our channels to claim files.</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return

    # ✅ Remove from waiting state
    current_waiting_for_code.discard(user_id)

    # ✅ Process claim code
    success, result = use_claim_code(user_id, code)

    if not success:
        error_msg = {
            "invalid_code": "❌ <b>Invalid or expired claim code!</b>\n\nPlease check the code and try again.",
            "no_files": "❌ <b>This claim code has no files left!</b>\n\nThe code has been automatically deactivated.",
            "already_claimed": "❌ <b>You have already claimed this code!</b>\n\nEach user can only use a code once."
        }.get(result, "❌ <b>Unknown error occurred.</b>")
        await update.message.reply_text(error_msg, parse_mode='HTML')
        return

    file_info = get_random_claim_file()
    if not file_info:
        await update.message.reply_text(
            "❌ <b>No files available for claim!</b>\n\nPlease contact admin.",
            parse_mode='HTML'
        )
        return

    safe_file_name = html.escape(file_info['file_name'])

    try:
        await context.bot.send_document(
            chat_id=user_id,
            document=file_info['file_id'],
            caption=(
                f"<b>✅ Code Claimed Successfully!</b>\n\n"
                f"📁 <b>File:</b> {safe_file_name}\n"
                f"📊 <b>Remaining uses for this code:</b> {result}"
            ),
            parse_mode='HTML'
        )

        log_claim(
            user_id=user_id,
            username=update.effective_user.username or "N/A",
            file_id=file_info['file_id'],
            file_name=file_info['file_name'],
            claim_code=code
        )

        delete_claim_file(file_info["_id"])

    except Exception as e:
        logger.error(f"Error sending file: {e}")
        await update.message.reply_text(
            (
                f"<b>✅ Code Claimed Successfully!</b>\n\n"
                f"❌ <b>Error sending file.</b> Please contact admin.\n"
                f"📊 <b>Remaining uses for this code:</b> {result}"
            ),
            parse_mode='HTML'
        )

        
async def handle_custom_code_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    custom_code = update.message.text.strip().upper()
    
    if len(custom_code) < 4:
        await update.message.reply_text("❌ **Code too short!**\n\nPlease enter a code with at least 4 characters.", parse_mode='Markdown')
        return
    
    # Get the number of users from pending data
    if user_id not in pending_code_data:
        await update.message.reply_text("❌ **Session expired!**\n\nPlease start again.", parse_mode='Markdown')
        current_waiting_for_custom_code.discard(user_id)
        return
    
    num_users = pending_code_data[user_id]
    
    # Check if enough claim files are available
    # Find this section in handle_custom_code_creation and replace:
# Check if enough claim files are available
    settings = get_bot_settings()
    if settings.get('claim_files', 0) < num_users:
     await update.message.reply_text(
        f"❌ **Not enough claim files!**\n\nYou need {num_users} claim files but only have {settings.get('claim_files', 0)}.\n\nPlease add more claim files first.", 
        parse_mode='Markdown'
     )
     current_waiting_for_custom_code.discard(user_id)
     del pending_code_data[user_id]
     return

# Try to create the custom code
    if create_claim_code(custom_code, num_users, OWNER_ID):
    # Don't reduce claim_files here - they will be reduced when users claim
     await update.message.reply_text(
        f"✅ **Custom Claim Code Created!**\n\n🔐 Code: `{custom_code}`\n👥 Max Users: {num_users}\n📁 Files per claim: 1\n\n💡 Share this code with users!",
        parse_mode='Markdown'
     )
    else:
     await update.message.reply_text("❌ **Code already exists!**\n\nPlease choose a different code.", parse_mode='Markdown')
     return
    
    current_waiting_for_custom_code.discard(user_id)
    del pending_code_data[user_id]

async def handle_code_users_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    try:
        num_users = int(update.message.text.strip())
        if num_users <= 0:
            await update.message.reply_text("❌ **Invalid number!**\n\nPlease enter a positive number.", parse_mode='Markdown')
            return
        
        # Store the number of users and move to next step
        pending_code_data[user_id] = num_users
        current_waiting_for_code_users.discard(user_id)
        current_waiting_for_custom_code.add(user_id)
        
        await update.message.reply_text(
            f"✅ **Step 1 Complete!**\n\n👥 Max users: {num_users}\n\n🔐 **Step 2:** Now send me the custom code you want to create (minimum 4 characters):",
            parse_mode='Markdown'
        )
        
    except ValueError:
        await update.message.reply_text("❌ **Invalid input!**\n\nPlease send a valid number.", parse_mode='Markdown')

import html

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, file_type: str):
    user_id = update.effective_user.id

    # Get file information
    file_obj = None
    file_name = "Unknown"

    if update.message.document:
        file_obj = update.message.document
        file_name = file_obj.file_name or "document"
    elif update.message.photo:
        file_obj = update.message.photo[-1]
        file_name = "photo.jpg"
    elif update.message.video:
        file_obj = update.message.video
        file_name = file_obj.file_name or "video.mp4"
    elif update.message.audio:
        file_obj = update.message.audio
        file_name = file_obj.file_name or "audio.mp3"
    elif update.message.voice:
        file_obj = update.message.voice
        file_name = "voice.ogg"
    elif update.message.video_note:
        file_obj = update.message.video_note
        file_name = "video_note.mp4"
    elif update.message.sticker:
        file_obj = update.message.sticker
        file_name = "sticker.webp"

    if file_obj:
        try:
            # Store file info in database
            store_file_info(file_obj.file_id, file_type, file_name, user_id)

            # Get current settings and update count
            settings = get_bot_settings()
            current_files = settings.get(file_type, 0)
            new_count = current_files + 1

            # Update file count
            update_bot_setting(file_type, new_count)

            # Fix file type name display
            file_type_name = "withdrawal" if file_type == "withdraw_files" else "claim code"

            # Escape file name for HTML
            safe_file_name = html.escape(file_name)

            await update.message.reply_text(
                f"<b>✅ File Added Successfully!</b>\n\n"
                f"📁 <b>File:</b> {safe_file_name}\n"
                f"📁 <b>File Type:</b> {file_type_name.title()}\n"
                f"📊 <b>Total {file_type_name} files:</b> {new_count}\n\n"
                f"💡 Send more files or go back to menu.",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error handling file upload: {e}")
            await update.message.reply_text(
                f"<b>❌ Error uploading file!</b>\n\n"
                f"Please try again or contact admin.\n"
                f"Error: {html.escape(str(e))}",
                parse_mode='HTML'
            )
    else:
        await update.message.reply_text(
            "<b>❌ Please send a valid file!</b>\n\n"
            "Supported: documents, photos, videos, audio, voice messages, video notes, stickers.",
            parse_mode='HTML'
        )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except telegram.error.BadRequest:
        pass  # Handles "query too old" errors

    user_id = query.from_user.id

    if await check_channel_membership(context.bot, user_id):
        await query.edit_message_text("✅ Thank you for joining our channels!", parse_mode='Markdown')

        referred_by = pending_referrals.pop(user_id, None)
        logger.info(f"[Referral Check] User: {user_id}, referred_by: {referred_by}")

        existing_user = get_user(user_id)
        if not existing_user:
            add_user(user_id, query.from_user.username, query.from_user.first_name, referred_by)

            if referred_by and referred_by != user_id:
                referrer = get_user(referred_by)
                if referrer:
                    update_user_points(referred_by, 8)
                    update_user_points(user_id, 4)

                    await context.bot.send_message(
                        user_id,
                        "🎉 You've earned 4 points for joining through a referral link!"
                    )
                    try:
                        await context.bot.send_message(
                            referred_by,
                            f"🎉 You earned 8 points! {query.from_user.first_name} joined using your referral link!"
                        )
                    except:
                        pass

        await asyncio.sleep(1)
        await show_main_menu(update, context)

    else:
        await query.answer("❌ Please join all required channels first!", show_alert=True)



async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Get user ID
    if update.callback_query:
        user_id = update.callback_query.from_user.id
    else:
        user_id = update.effective_user.id

    # Remove user from all waiting states
    current_waiting_for_code.discard(user_id)
    current_waiting_for_custom_code.discard(user_id)
    current_waiting_for_withdraw_files.discard(user_id)
    current_waiting_for_claim_files.discard(user_id)
    current_waiting_for_code_users.discard(user_id)
    if user_id in pending_code_data:
        del pending_code_data[user_id]

    # ✅ Delete the previous button message
    try:
        if update.callback_query:
            await update.callback_query.message.delete()
    except Exception as e:
        logger.warning(f"Failed to delete back-to-menu message: {e}")

    # Show the main menu
    await show_main_menu(update, context)


# Message handler for claim codes, custom code creation, file uploads, and number inputs

# Message handler portion for file uploads
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Safely skip updates that have no message (e.g., callback queries)
    if not update.message or not update.message.chat:
        return

    user_id = update.effective_user.id
    # Ignore group/channel messages
    if update.message.chat.type != "private":
     return

    # Check if user is waiting to enter a claim code (FIRST PRIORITY)
    if user_id in current_waiting_for_code_users:
        if update.message.text and not (
          update.message.document or update.message.photo or update.message.video or 
          update.message.audio or update.message.voice or update.message.video_note or 
          update.message.sticker
    ):
          await handle_code_users_input(update, context)
        else:
            await update.message.reply_text("❌ **Please send a number only!**\n\nDon't send files here, just type a number.", parse_mode='Markdown')
        return

# ✅ Check if user is waiting to enter a claim code (SECOND)
    if user_id in current_waiting_for_code:
        if update.message.text and not (
          update.message.document or update.message.photo or update.message.video or 
          update.message.audio or update.message.voice or update.message.video_note or 
          update.message.sticker
    ):
          if len(update.message.text.strip()) >= 4:
            await handle_claim_code(update, context)
          else:
            await update.message.reply_text("❌ **Invalid claim code!**\n\nPlease enter a valid claim code (at least 4 characters).", parse_mode='Markdown')
        else:
         await update.message.reply_text("❌ **Please send the claim code as text!**\n\nDon't send files here, just type your claim code.", parse_mode='Markdown')
        return
    
   
    # Check if owner is waiting to create a custom code (THIRD)
    if user_id in current_waiting_for_custom_code:
        if update.message.text and not (update.message.document or update.message.photo or update.message.video or update.message.audio or update.message.voice or update.message.video_note or update.message.sticker):
            if len(update.message.text.strip()) >= 4:
                await handle_custom_code_creation(update, context)
            else:
                await update.message.reply_text("❌ **Invalid code format!**\n\nPlease enter a code with at least 4 characters.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ **Please send text only!**\n\nDont send files here, just type your custom code.", parse_mode='Markdown')
        return
    
    # Check if owner is uploading withdrawal files (FOURTH)
    if user_id in current_waiting_for_withdraw_files:
        if update.message.document or update.message.photo or update.message.video or update.message.audio or update.message.voice or update.message.video_note or update.message.sticker:
            await handle_file_upload(update, context, "withdraw_files")
        elif update.message.text:
            await update.message.reply_text("❌ **Please send a file, not text!**\n\nYou can send documents, photos, videos, audio files, voice messages, video notes, or stickers.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ **Please send a file!**\n\nYou can send documents, photos, videos, audio files, voice messages, video notes, or stickers.", parse_mode='Markdown')
        return
    
    # Check if owner is uploading claim files (FIFTH)
    if user_id in current_waiting_for_claim_files:
        if update.message.document or update.message.photo or update.message.video or update.message.audio or update.message.voice or update.message.video_note or update.message.sticker:
            await handle_file_upload(update, context, "claim_files")
        elif update.message.text:
            await update.message.reply_text("❌ **Please send a file, not text!**\n\nYou can send documents, photos, videos, audio files, voice messages, video notes, or stickers.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ **Please send a file!**\n\nYou can send documents, photos, videos, audio files, voice messages, video notes, or stickers.", parse_mode='Markdown')
        return
    
    # Handle regular text messages
    if update.message.text:
        # Check channel membership for existing users
        if not await check_channel_membership(context.bot, user_id):
            keyboard = []
            for channel in REQUIRED_CHANNELS:
                keyboard.append([InlineKeyboardButton(f"Join {channel['name']}", url=f"https://t.me/{channel['id'][1:]}")])
            keyboard.append([InlineKeyboardButton("✅ I Joined", callback_data="check_membership")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "❌ **You must join our channels to continue using the bot:**",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text("Please use the menu buttons or click 'Claim Code' to enter a code.")
    
    # Handle files sent outside of expected contexts
    elif update.message.document or update.message.photo or update.message.video or update.message.audio:
        await update.message.reply_text("❌ **Unexpected file!**\n\nPlease use the menu options first before sending files.", parse_mode='Markdown')


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user:
        await start(update, context)
        return
    
    # Create reply keyboard buttons that will appear in chat box
    keyboard = [
        [KeyboardButton("👤 My Profile"), KeyboardButton("⚡ Withdraw Points")],
        [KeyboardButton("🎁 Claim Code"), KeyboardButton("📊 Stats")]
    ]
    
    # Add owner-only buttons
    if update.effective_user.id == OWNER_ID:
        keyboard.append([KeyboardButton("🔐 Generate Code (Owner)")])
        keyboard.append([KeyboardButton("📁 Add Files (Owner)")])
    
    # Create reply keyboard markup
    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,  # Makes buttons smaller
        one_time_keyboard=False,  # Keeps buttons visible
        input_field_placeholder="Choose an option..."  # Placeholder text
    )
    
    message_text = f"🏠 Main Menu\n\nWelcome back, {update.effective_user.first_name}!"
    
    if update.callback_query:
        await update.callback_query.message.reply_text(message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup)

async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    # Create a mock callback query object to reuse existing functions
    class MockCallbackQuery:
        def __init__(self, user, message):
            self.from_user = user
            self.message = message
            
        async def answer(self):
            pass
            
        async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
            await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    
    # Create mock callback query
    mock_query = MockCallbackQuery(update.effective_user, update.message)
    mock_update = type('MockUpdate', (), {
        'callback_query': mock_query,
        'effective_user': update.effective_user
    })()
    
    # Route to appropriate function based on button text
    if text == "👤 My Profile":
        await my_profile(mock_update, context)
    elif text == "⚡ Withdraw Points":
        await withdraw_points(mock_update, context)
    elif text == "🎁 Claim Code":
        await claim_code_menu(mock_update, context)
    elif text == "📊 Stats":
        await stats(mock_update, context)
    elif text == "🔐 Generate Code (Owner)" and update.effective_user.id == OWNER_ID:
        await generate_code_menu(mock_update, context)
    elif text == "📁 Add Files (Owner)" and update.effective_user.id == OWNER_ID:
        await add_files_menu(mock_update, context)

async def auto_delete_message_after_delay(message, delay_seconds=10):
    """Helper function to auto-delete messages after a delay"""
    await asyncio.sleep(delay_seconds)
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Error auto-deleting message: {e}")

# Replace the callback handlers in your main() function with these:

# Add these callback handlers for file uploads
async def add_withdraw_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_files(update, context, "withdraw_files")

async def add_claim_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_files(update, context, "claim_files")
    
def main():
    # ✅ Initialize application before using it
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(my_profile, pattern="my_profile"))
    application.add_handler(CallbackQueryHandler(withdraw_points, pattern="withdraw_points"))
    application.add_handler(CallbackQueryHandler(claim_code_menu, pattern="claim_code"))
    application.add_handler(CallbackQueryHandler(stats, pattern="stats"))
    application.add_handler(CallbackQueryHandler(generate_code_menu, pattern="generate_code"))
    application.add_handler(CallbackQueryHandler(add_files_menu, pattern="add_files"))
    application.add_handler(CallbackQueryHandler(add_withdraw_files_handler, pattern="add_withdraw_files"))
    application.add_handler(CallbackQueryHandler(add_claim_files_handler, pattern="add_claim_files"))
    application.add_handler(CallbackQueryHandler(check_membership, pattern="check_membership"))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern="back_to_menu"))
    application.add_handler(MessageHandler(filters.Regex(r"^(👤 My Profile|⚡ Withdraw Points|🎁 Claim Code|📊 Stats|🔐 Generate Code \(Owner\)|📁 Add Files \(Owner\))$"), handle_keyboard_buttons))
    application.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
        handle_message
    ))

    # Set the webhook to Render domain
    webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/{BOT_TOKEN}"
    asyncio.run(bot.set_webhook(url=webhook_url))

    # Start Flask app on port 8080
    app.run(host="0.0.0.0", port=8080)

# Entry point
if __name__ == "__main__":
    main()
