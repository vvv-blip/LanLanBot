import os
import json
import logging
import requests
import asyncio
import random
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# Ensure /data directory exists
os.makedirs("/data", exist_ok=True)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("/data/bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY")
MAMA_COIN_ADDRESS = os.getenv("MAMA_COIN_ADDRESS")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", 8080))

try:
    TOTAL_SUPPLY = float(os.getenv("TOTAL_SUPPLY", 8888888888.0))
except (ValueError, TypeError):
    TOTAL_SUPPLY = 8888888888.0
    logger.warning("TOTAL_SUPPLY invalid, using default: %s", TOTAL_SUPPLY)

# Validate environment variables
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is required")
if not THEGRAPH_API_KEY:
    raise ValueError("THEGRAPH_API_KEY is required")
if not MAMA_COIN_ADDRESS:
    raise ValueError("MAMA_COIN_ADDRESS is required")
if not WEBHOOK_URL:
    raise ValueError("WEBHOOK_URL is required")

# Constants
SETTINGS_FILE = "/data/settings.json"
GROUPS_FILE = "/data/groups.json"
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id/EYCKATKGBKLWvSfwvBjzfCBmGwYNdVkduYXVivCsLRFu"

# Load SCHEDULED_INTERVAL
SCHEDULED_INTERVAL_STR = os.getenv("SCHEDULED_INTERVAL", "2h")
def parse_interval_string(interval_str):
    if not isinstance(interval_str, str):
        return None
    match_h = re.match(r'(\d+)\s*h', interval_str)
    match_m = re.match(r'(\d+)\s*m', interval_str)
    if match_h:
        return int(match_h.group(1)) * 3600
    elif match_m:
        return int(match_m.group(1)) * 60
    return None

SCHEDULED_INTERVAL = parse_interval_string(SCHEDULED_INTERVAL_STR) or 7200
SCHEDULED_FIRST = 60

# Image URLs
DEFAULT_IMAGE_URL = "https://i.imgur.com/LFE9ouI.jpeg"
SCHEDULED_AND_CHECK_PRICE_IMAGE_URL = "https://i.imgur.com/EkpFRCD.jpeg"
GROWTH_GIF_URLS = [
    "https://i.imgur.com/growth1.gif",  # Replace with actual direct links
    "https://i.imgur.com/growth2.gif",
    "https://i.imgur.com/growth3.gif",
    "https://i.imgur.com/growth4.gif",
    "https://i.imgur.com/growth5.gif",
]
MILESTONE_GIF_URLS = [
    "https://i.imgur.com/milestone1.gif",  # Replace with actual direct links
    "https://i.imgur.com/milestone2.gif",
    "https://i.imgur.com/milestone3.gif",
    "https://i.imgur.com/milestone4.gif",
    "https://i.imgur.com/milestone5.gif",
]

# Data persistence
def load_json(file_path, default_value):
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
            if isinstance(default_value, dict) and not isinstance(data, dict):
                logger.warning(f"Loaded data from {file_path} is not a dict")
                return default_value
            if isinstance(default_value, list) and not isinstance(data, list):
                logger.warning(f"Loaded data from {file_path} is not a list")
                return default_value
            return data
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load {file_path}: {e}")
        return default_value
    except Exception as e:
        logger.error(f"Unexpected error loading {file_path}: {e}")
        return default_value

def save_json(file_path, data):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        logger.info(f"Writing to {file_path}")
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
        logger.info(f"Successfully wrote to {file_path}")
    except Exception as e:
        logger.error(f"Failed to save {file_path}: {e}")

# Global variables
settings = {}
groups = set()
last_known_market_cap = None
current_investment_example_index = 0
INVESTMENT_EXAMPLES = [100, 1000, 10000]

# Progress bar
def generate_progress_bar(current_value, start_milestone, end_milestone, bar_length=10):
    if end_milestone <= start_milestone:
        return "[██████████] **100%**" if current_value >= end_milestone else "[Error: Invalid Milestones]"
    progress_range = end_milestone - start_milestone
    normalized_value = max(0, current_value - start_milestone)
    progress_percentage = min(100, (normalized_value / progress_range) * 100)
    filled_blocks = int(bar_length * (progress_percentage / 100))
    bar = "█" * filled_blocks + "░" * (bar_length - filled_blocks)
    return f"[{bar}] **{progress_percentage:.0f}%**"

# Fetch market cap with retry
def fetch_market_cap():
    if not SUBGRAPH_URL or not MAMA_COIN_ADDRESS:
        logger.error("SUBGRAPH_URL or MAMA_COIN_ADDRESS missing")
        return None
    query = """
    {
      token(id: "%s") {
        id
        derivedETH
      }
      bundle(id: "1") {
        ethPrice
      }
    }
    """ % MAMA_COIN_ADDRESS.lower()
    for attempt in range(3):
        try:
            logger.info(f"Fetching market cap, attempt {attempt + 1}")
            response = requests.post(SUBGRAPH_URL, json={"query": query}, timeout=15)
            response.raise_for_status()
            data = response.json()["data"]
            if "errors" in response.json():
                logger.error(f"Subgraph errors: {response.json()['errors']}")
                return None
            token_data = data.get("token")
            if not token_data:
                logger.error(f"No token data for {MAMA_COIN_ADDRESS.lower()}")
                return None
            eth_price_usd = float(data["bundle"]["ethPrice"])
            token_price_eth = float(token_data["derivedETH"])
            token_price_usd = token_price_eth * eth_price_usd
            market_cap = token_price_usd * TOTAL_SUPPLY
            logger.info(f"Fetched market cap: ${market_cap:,.0f}")
            return market_cap
        except requests.exceptions.RequestException as req_err:
            logger.warning(f"Attempt {attempt + 1} failed: {req_err}")
            asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return None
    logger.error("All retry attempts failed")
    return None

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    groups.add(chat_id)
    save_json(GROUPS_FILE, list(groups))
    logger.info(f"Group {chat_id} started bot")
    keyboard = [
        [InlineKeyboardButton("🚀 Check LanLan Price", callback_data='check_lanlan_price')],
        [InlineKeyboardButton("🤔 Calculate My Investment", callback_data='start_lanlan_calculation')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_photo(
        photo=DEFAULT_IMAGE_URL,
        caption="🎉 Hey, LanLan lovers! 😺 I’m your bubbly bot tracking LanLan’s purr-gress! Choose an option below to get started. 🌟",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "🐾 Here are the commands you can use with LanLan Bot:\n\n"
        "• `/start`: Get started and see the main menu.\n"
        "• `/lanlan <investment> <initial_market_cap>`: Calculate your potential gains. "
        "Example: `/lanlan 100 5000000`.\n"
        "• `/wen`: A fun check on LanLan's readiness for takeoff!\n"
        "• `/whomadethebot`: Find out who crafted this bot.\n\n"
        "**Admin Commands (Group Admins Only):**\n"
        "• `/setschedule <interval>`: Set update interval (e.g., `/setschedule 1h`).\n\n"
        "Orange is the new Cat! 🍊🐾"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == 'check_lanlan_price':
        await query.message.reply_text("🐾 Fetching LanLan deets...")
        await lanlan_price_status(query, context)
    elif query.data == 'start_lanlan_calculation':
        await query.message.reply_text(
            "Ready to crunch numbers? Type `/lanlan <amount_invested> <initial_market_cap>` (e.g., `/lanlan 100 5000000`)."
        )
    elif query.data == 'back_to_main':
        await query.message.delete()
        dummy_update = Update(update_id=update.update_id)
        dummy_update._effective_chat = query.message.chat
        dummy_update._effective_message = query.message
        await start(dummy_update, context)

async def lanlan_price_status(update_object: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    market_cap = fetch_market_cap()
    if market_cap is None:
        await update_object.effective_message.reply_text("😿 Couldn’t fetch LanLan data! Try again later.")
        return
    if TOTAL_SUPPLY == 0:
        await update_object.effective_message.reply_text("😿 Total supply is zero!")
        return
    price = market_cap / TOTAL_SUPPLY
    milestones = [
        10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000,
        100_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000,
        1_000_000_000, 1_500_000_000, 2_000_000_000, 5_000_000_000, 10_000_000_000
    ]
    highest_achieved = settings.get('highest_milestone_achieved', 0)
    current_milestone_start = highest_achieved
    next_milestone_end = next((m for m in sorted(milestones) if m > highest_achieved), None)
    if next_milestone_end is None:
        current_milestone_start = milestones[-1] if milestones else 0
        next_milestone_end = current_milestone_start * 1.5 if current_milestone_start else 10_000_000
    if market_cap < current_milestone_start:
        current_milestone_start = max([m for m in milestones if m <= market_cap] + [0])
        next_milestone_end = next((m for m in sorted(milestones) if m > market_cap), current_milestone_start * 1.5)
    progress_bar = generate_progress_bar(market_cap, current_milestone_start, next_milestone_end)
    message = (
        f"🌟 LanLan is purring!\n"
        f"Market Cap: **${market_cap:,.0f}**\n"
        f"Price: **${price:,.10f}**\n\n"
        f"Next Target: **${next_milestone_end:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"Oranga is the new Cat! 🍊🐾"
    )
    keyboard = [
        [InlineKeyboardButton("🤔 Calculate My Investment", callback_data='start_lanlan_calculation')],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update_object.effective_message.reply_photo(
            photo=SCHEDULED_AND_CHECK_PRICE_IMAGE_URL,
            caption=message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"Could not send image: {e}")
        await update_object.effective_message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)

async def lanlan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await update.message.reply_text(
            "Meow! Use: `/lanlan <amount_invested> <initial_market_cap>` (e.g., `/lanlan 100 5000000`)."
        )
        return
    try:
        investment = float(context.args[0])
        initial_market_cap = float(context.args[1])
        if investment <= 0 or initial_market_cap <= 0:
            await update.message.reply_text("Please use positive numbers!")
            return
        current_market_cap = fetch_market_cap()
        if current_market_cap is None:
            await update.message.reply_text("😿 Couldn’t fetch LanLan data!")
            return
        if TOTAL_SUPPLY == 0:
            await update.message.reply_text("😿 Total supply is zero!")
            return
        initial_price = initial_market_cap / TOTAL_SUPPLY
        current_price = current_market_cap / TOTAL_SUPPLY
        if initial_price == 0:
            await update.message.reply_text("😿 Initial price was zero!")
            return
        tokens = investment / initial_price
        current_value = tokens * current_price
        future_projections = []
        for target_cap in [100_000_000, 500_000_000, 1_000_000_000]:
            target_price = target_cap / TOTAL_SUPPLY
            future_value = tokens * target_price
            future_projections.append(f"at **${target_cap:,.0f}** market cap, that's **${future_value:,.2f}**")
        keyboard = [
            [InlineKeyboardButton("🚀 Check LanLan Price Now", callback_data='check_lanlan_price')],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = (
            f"🎉 If you invested **${investment:,.2f}** at **${initial_market_cap:,.0f}** market cap, "
            f"you’d have **{tokens:,.2f}** tokens.\n\n"
            f"At **${current_market_cap:,.0f}** market cap, your investment is worth **${current_value:,.2f}**.\n\n"
            f"Future gains:\n" + "\n".join(future_projections) + "!\n\n"
            f"Get ready for a cat-tastic ride! 🚀😺"
        )
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
    except ValueError:
        await update.message.reply_text("Please enter valid numbers (e.g., `/lanlan 100 5000000`).")
    except Exception as e:
        logger.error(f"Error in lanlan command: {e}")
        await update.message.reply_text("😿 An error occurred!")

async def wen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("😺 Meow meow! LanLan is ready to soar, are you? 🚀🧲 Oranga is the new Cat!")

async def whomadethebot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("@nakatroll")

async def setimage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Image settings are hardcoded. Contact a developer for changes.")

async def setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global SCHEDULED_INTERVAL, SCHEDULED_INTERVAL_STR
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text("😺 Use this in group chats as an admin!")
        return
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        if not any(admin.user.id == user_id for admin in admins):
            await update.message.reply_text("😺 Only group admins can set the schedule!")
            return
        if len(context.args) != 1:
            await update.message.reply_text("😺 Usage: `/setschedule <interval>` (e.g., `2h`, `30m`).")
            return
        new_interval_str = context.args[0]
        new_interval_seconds = parse_interval_string(new_interval_str)
        if new_interval_seconds is None or new_interval_seconds <= 0:
            await update.message.reply_text("Use formats like `2h` or `30m`!")
            return
        SCHEDULED_INTERVAL = new_interval_seconds
        SCHEDULED_INTERVAL_STR = new_interval_str
        job_queue: JobQueue = context.application.job_queue
        for job in job_queue.get_jobs_by_name("scheduled_price_update"):
            job.schedule_removal()
        job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
        logger.info(f"Scheduled job updated to interval: {SCHEDULED_INTERVAL_STR}")
        await update.message.reply_text(f"🎉 Updates every **{SCHEDULED_INTERVAL_STR.replace('h', ' hours').replace('m', ' minutes')}**!")
    except Exception as e:
        logger.error(f"Error in setschedule: {e}")
        await update.message.reply_text("😿 Error setting schedule!")

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_known_market_cap, current_investment_example_index, settings
    market_cap = fetch_market_cap()
    if market_cap is None:
        logger.warning("Scheduled job skipped due to fetch failure")
        return
    if TOTAL_SUPPLY == 0:
        logger.warning("TOTAL_SUPPLY is zero")
        return
    price = market_cap / TOTAL_SUPPLY
    milestones = [
        10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000,
        100_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000,
        1_000_000_000, 1_500_000_000, 2_000_000_000, 5_000_000_000, 10_000_000_000
    ]
    highest_milestone_achieved = settings.get('highest_milestone_achieved', 0)
    for milestone_val in sorted(milestones):
        if market_cap >= milestone_val > highest_milestone_achieved:
            highest_milestone_achieved = milestone_val
            settings['highest_milestone_achieved'] = highest_milestone_achieved
            save_json(SETTINGS_FILE, settings)
    current_milestone_start = highest_milestone_achieved
    next_milestone_end = next((m for m in sorted(milestones) if m > highest_milestone_achieved), None)
    if next_milestone_end is None:
        current_milestone_start = milestones[-1] if milestones else 0
        next_milestone_end = current_milestone_start * 1.5 if current_milestone_start else 10_000_000
    progress_bar = generate_progress_bar(market_cap, current_milestone_start, next_milestone_end)
    if last_known_market_cap is not None:
        for milestone_cap in sorted(milestones):
            if last_known_market_cap < milestone_cap <= market_cap:
                milestone_message = (
                    f"✨🎉 WoW! LanLan crossed **${milestone_cap:,.0f}** market cap! "
                    f"Current: **${market_cap:,.0f}** 🚀😺"
                )
                for group_id in list(groups):
                    try:
                        await context.bot.send_photo(
                            chat_id=group_id,
                            photo=random.choice(MILESTONE_GIF_URLS),
                            caption=milestone_message,
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send milestone to {group_id}: {e}")
    last_known_market_cap = market_cap
    investment_amount = INVESTMENT_EXAMPLES[current_investment_example_index]
    current_investment_example_index = (current_investment_example_index + 1) % len(INVESTMENT_EXAMPLES)
    initial_market_cap = 5_000_000
    initial_price = initial_market_cap / TOTAL_SUPPLY if TOTAL_SUPPLY else 0
    tokens_at_initial = investment_amount / initial_price if initial_price else 0
    current_value = tokens_at_initial * price
    tokens_now = investment_amount / price if price else 0
    future_value_messages = []
    for target_cap in [100_000_000, 500_000_000, 1_000_000_000]:
        target_price = target_cap / TOTAL_SUPPLY if TOTAL_SUPPLY else 0
        value_at_target = tokens_now * target_price
        future_value_messages.append(f"• at **${target_cap:,.0f}** MC: **${value_at_target:,.2f}**")
    buy_now_message = f"If you bought **${investment_amount:,.0f}** today:\n" + "\n".join(future_value_messages)
    message = (
        f"🌟 LanLan is purring!\n"
        f"**MC:** **${market_cap:,.0f}** | **Price:** **${price:,.10f}**\n"
        f"**Next Target:** **${next_milestone_end:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"📈 Invested **${investment_amount:,.0f}** at **${initial_market_cap:,.0f}** MC? "
        f"Now worth **${current_value:,.2f}**!\n"
        f"{buy_now_message}\n\n"
        f"Orange is the new Cat! 🍊🐾"
    )
    for group_id in list(groups):
        try:
            await context.bot.send_photo(
                chat_id=group_id,
                photo=SCHEDULED_AND_CHECK_PRICE_IMAGE_URL,
                caption=message,
                parse_mode='Markdown'
            )
            logger.info(f"Sent update to group {group_id}")
        except Exception as e:
            logger.warning(f"Failed to send to group {group_id}: {e}")

# FastAPI app
app = FastAPI()
application = None

@app.on_event("startup")
async def startup_event():
    global application, settings, groups, last_known_market_cap
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .job_queue(JobQueue())
        .build()
    )
    await application.initialize()
    logger.info("Application initialized")
    settings = load_json(SETTINGS_FILE, {"highest_milestone_achieved": 0})
    settings["default_image_url"] = DEFAULT_IMAGE_URL
    settings["scheduled_and_check_price_image_url"] = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    save_json(SETTINGS_FILE, settings)
    groups_list = load_json(GROUPS_FILE, [])
    groups = set(groups_list)
    initial_mc = fetch_market_cap()
    last_known_market_cap = initial_mc if initial_mc is not None else 0
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("wen", wen))
    application.add_handler(CommandHandler("lanlan", lanlan_command))
    application.add_handler(CommandHandler("setimage", setimage))
    application.add_handler(CommandHandler("setschedule", setschedule))
    application.add_handler(CommandHandler("whomadethebot", whomadethebot))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler, pattern='^(check_lanlan_price|start_lanlan_calculation|back_to_main)$'))
    job_queue = application.job_queue
    if SCHEDULED_INTERVAL > 0:
        job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
        logger.info(f"Scheduled job set with interval: {SCHEDULED_INTERVAL_STR}")
    full_webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await application.bot.set_webhook(url=full_webhook_url, allowed_updates=["message", "callback_query"])
    logger.info(f"Webhook set to: {full_webhook_url}")

@app.on_event("shutdown")
async def shutdown_event():
    if application:
        await application.shutdown()
        logger.info("Application shutdown")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if not application:
        logger.error("Application not initialized")
        return JSONResponse({"status": "error", "message": "Bot not ready"}, status_code=503)
    try:
        update_json = await request.json()
        await application.process_update(Update.de_json(update_json, application.bot))
        return JSONResponse({"status": "ok"}, status_code=200)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return JSONResponse({"status": "error", "message": "Processing failed"}, status_code=500)

@app.get("/")
async def home():
    return "LanLan Bot is running!"

@app.get("/health")
async def health_check():
    return JSONResponse({"status": "healthy", "message": "Bot operational"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
