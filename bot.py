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
    ConversationHandler
)
from flask import Flask, request, jsonify

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# --- Environment variables (NO HARDCODED DEFAULTS FOR SENSITIVE KEYS) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY")
MAMA_COIN_ADDRESS = os.getenv("MAMA_COIN_ADDRESS")

try:
    TOTAL_SUPPLY = float(os.getenv("TOTAL_SUPPLY"))
except (ValueError, TypeError):
    TOTAL_SUPPLY = 8888888888.0
    logger.warning("TOTAL_SUPPLY environment variable not set or invalid, using default: %s", TOTAL_SUPPLY)

# --- WEBHOOK CONFIGURATION ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/webhook"
# PORT will be handled by Uvicorn/Gunicorn's binding

# Function to parse interval strings (e.g., "1h", "30m")
def parse_interval_string(interval_str):
    if not isinstance(interval_str, str):
        return None

    match_h = re.match(r'(\d+)\s*h', interval_str)
    match_m = re.match(r'(\d+)\s*m', interval_str)
    
    if match_h:
        return int(match_h.group(1)) * 3600
    elif match_m:
        return int(match_m.group(1)) * 60
    else:
        return None

# Constants
SETTINGS_FILE = "settings.json" 
GROUPS_FILE = "groups.json"

if not THEGRAPH_API_KEY:
    logger.critical("THEGRAPH_API_KEY is not set. Cannot form SUBGRAPH_URL.")
    SUBGRAPH_URL = None
else:
    SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id/EYCKATKGBKLWvSfwvBjzfCBmGwYNdVkduYXVivCsLRFu"

SCHEDULED_INTERVAL_STR = os.getenv("SCHEDULED_INTERVAL", "2h")
SCHEDULED_INTERVAL = parse_interval_string(SCHEDULED_INTERVAL_STR)
if SCHEDULED_INTERVAL is None:
    SCHEDULED_INTERVAL = 7200
    logger.warning(f"Invalid SCHEDULED_INTERVAL format '{SCHEDULED_INTERVAL_STR}', using default: {SCHEDULED_INTERVAL} seconds.")

SCHEDULED_FIRST = 60

# --- IMAGE URLs ---
DEFAULT_IMAGE_URL = "https://i.imgur.com/LFE9ouI.jpeg"
SCHEDULED_AND_CHECK_PRICE_IMAGE_URL = "https://i.imgur.com/EkpFRCD.jpeg"

GROWTH_GIF_URLS = [
    "https://i.imgur.com/growth1.gif", 
    "https://i.imgur.com/growth2.gif",
    "https://i.imgur.com/growth3.gif",
    "https://i.imgur.com/growth4.gif",
    "https://i.imgur.com/growth5.gif",
]
MILESTONE_GIF_URLS = [
    "https://i.imgur.com/milestone1.gif", 
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
                logger.warning(f"Loaded data from {file_path} is not a dict, returning default.")
                return default_value
            if isinstance(default_value, list) and not isinstance(data, list):
                logger.warning(f"Loaded data from {file_path} is not a list, returning default.")
                return default_value
            return data
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load {file_path}: {e}. Using default.")
        return default_value
    except Exception as e:
        logger.error(f"An unexpected error occurred while loading {file_path}: {e}. Using default.")
        return default_value

def save_json(file_path, data):
    try:
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save {file_path}: {e}")

settings = {} 
groups = set()
last_known_market_cap = None
current_investment_example_index = 0
INVESTMENT_EXAMPLES = [100, 1000, 10000]

def generate_progress_bar(current_value, start_milestone, end_milestone, bar_length=10):
    if end_milestone <= start_milestone:
        if current_value >= end_milestone:
            return "[██████████] **100%**"
        return "[Error: Invalid Milestones]"
    
    progress_range = end_milestone - start_milestone
    normalized_value = current_value - start_milestone
    
    if normalized_value < 0:
        progress_percentage = 0
    else:
        progress_percentage = min(100, (normalized_value / progress_range) * 100)
    
    filled_blocks = int(bar_length * (progress_percentage / 100))
    empty_blocks = bar_length - filled_blocks
    
    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"[{bar}] **{progress_percentage:.0f}%**"

def fetch_market_cap():
    if not SUBGRAPH_URL or not MAMA_COIN_ADDRESS:
        logger.error("SUBGRAPH_URL or MAMA_COIN_ADDRESS is not defined. Cannot fetch market cap.")
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

    try:
        logger.info(f"Fetching market cap for token ID: {MAMA_COIN_ADDRESS.lower()}")
        response = requests.post(SUBGRAPH_URL, json={"query": query}, timeout=15)
        response.raise_for_status()
        
        data = response.json()["data"]
        
        if "errors" in response.json():
            logger.error(f"Subgraph returned errors: {response.json()['errors']}")
            return None

        token_data = data.get("token")
        if not token_data:
            logger.error(f"No token data found for LanLan token with ID: {MAMA_COIN_ADDRESS.lower()}")
            return None
        
        eth_price_usd = float(data["bundle"]["ethPrice"])
        token_price_eth = float(token_data["derivedETH"])

        token_price_usd = token_price_eth * eth_price_usd 
        market_cap = token_price_usd * TOTAL_SUPPLY
        logger.info(f"Fetched market cap: ${market_cap:,.0f}")
        return market_cap
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Network or HTTP error fetching market cap: {req_err}")
    except json.JSONDecodeError as json_err:
        logger.error(f"JSON decode error from subgraph response: {json_err}. Response: {response.text if 'response' in locals() else 'N/A'}")
    except KeyError as key_err:
        logger.error(f"Key error in subgraph data structure: {key_err}. Data: {data if 'data' in locals() else 'N/A'}")
    except Exception as e:
        logger.error(f"An unexpected error occurred fetching market cap: {e}")
    return None

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
        caption=(
            "🎉 Hey, LanLan lovers! 😺 I’m your bubbly bot tracking LanLan’s purr-gress! "
            "Choose an option below to get started. 🌟"
        ),
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "🐾 Here are the commands you can use with LanLan Bot:\n\n"
        "• `/start`: Get started and see the main menu.\n"
        "• `/lanlan <investment> <initial_market_cap>`: Calculate your potential gains. "
        "Example: `/lanlan 100 5000000` (meaning **$100** invested at **$5,000,000** market cap).\n"
        "• `/wen`: A fun check on LanLan's readiness for takeoff!\n"
        "• `/whomadethebot`: Find out who crafted this purr-fect bot.\n\n"
        "**Admin Commands (Group Admins Only):**\n"
        "• `/setschedule <interval>`: Set how often scheduled updates are sent. "
        "Example: `/setschedule 1h` or `/setschedule 30m`\n\n"
        "Remember, Orange is the new Cat! 🍊🐾"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == 'check_lanlan_price':
        await query.message.reply_text("🐾 Fetching the freshest LanLan deets for you... One moment! 🐱")
        await lanlan_price_status(query, context)
    elif query.data == 'start_lanlan_calculation':
        await query.message.reply_text(
            "Ready to crunch some numbers?!\n"
            "Just type `/lanlan <amount_invested> <initial_market_cap>`\n"
            "For example: `/lanlan 100 5000000` (meaning **$100** invested at **$5,000,000** market cap). Easy peasy, lemon squeezy! 🍋"
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
        await update_object.effective_message.reply_text("😿 Oh no, I couldn’t fetch LanLan data! Please try again later. The cat's on a coffee break!")
        return
    
    if TOTAL_SUPPLY == 0:
        await update_object.effective_message.reply_text("😿 Total supply is zero, so I can't calculate the price. Meow-ch!")
        return

    price = market_cap / TOTAL_SUPPLY

    milestones = [
        10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000,
        100_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000,
        1_000_000_000, 1_500_000_000, 2_000_000_000, 5_000_000_000, 10_000_000_000
    ]
    
    highest_achieved = settings.get('highest_milestone_achieved', 0)
    
    current_milestone_start_for_progress = highest_achieved
    next_milestone_end_for_progress = None

    for milestone_val in sorted(milestones):
        if milestone_val > highest_achieved:
            next_milestone_end_for_progress = milestone_val
            break
    
    if next_milestone_end_for_progress is None:
        if milestones:
            current_milestone_start_for_progress = milestones[-1]
            next_milestone_end_for_progress = current_milestone_start_for_progress * 1.5
        else:
            current_milestone_start_for_progress = 0
            next_milestone_end_for_progress = 10_000_000

    if market_cap < current_milestone_start_for_progress:
        temp_start = 0
        for m in sorted(milestones):
            if m <= market_cap:
                temp_start = m
            else:
                break
        current_milestone_start_for_progress = temp_start
        for m in sorted(milestones):
            if m > market_cap:
                next_milestone_end_for_progress = m
                break
        if next_milestone_end_for_progress is None:
            next_milestone_end_for_progress = current_milestone_start_for_progress * 1.5 if current_milestone_start_for_progress > 0 else 10_000_000

    progress_bar = generate_progress_bar(market_cap, current_milestone_start_for_progress, next_milestone_end_for_progress)

    message = (
        f"🌟 LanLan is currently purring! 😺\n"
        f"Current Market Cap: **${market_cap:,.0f}**\n"
        f"Current Price: **${price:,.10f}**\n\n"
        f"Next Target: **${next_milestone_end_for_progress:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"Oranga is the new Cat! 🍊🐾"
    )
    
    keyboard = [
        [InlineKeyboardButton("🤔 Calculate My Investment", callback_data='start_lanlan_calculation')],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    image_to_send = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL 

    try:
        target_message = update_object.effective_message 
        await target_message.reply_photo(
            photo=image_to_send,
            caption=message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"Could not send image for check price status, sending text only: {e}")
        target_message = update_object.effective_message
        await target_message.reply_text(
            message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

async def lanlan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 2:
        await update.message.reply_text(
            "Meow! To calculate your investment, please use:\n"
            "`/lanlan <amount_invested> <initial_market_cap>`\n"
            "For example: `/lanlan 100 5000000` (meaning **$100** invested at **$5,000,000** market cap)."
        )
        return

    try:
        investment = float(context.args[0])
        initial_market_cap = float(context.args[1])

        if investment <= 0 or initial_market_cap <= 0:
            await update.message.reply_text("Please enter positive numbers for both investment and initial market cap. Let's keep it purr-fect!")
            return

        current_market_cap = fetch_market_cap()
        if current_market_cap is None:
            await update.message.reply_text("😿 Oh no, I couldn’t fetch current LanLan data! Please try again later. The cat's on a coffee break!")
            return
        
        if TOTAL_SUPPLY == 0:
            await update.message.reply_text("😿 Total supply is zero, so I can't calculate potential gains. Meow-ch!")
            return

        initial_price = initial_market_cap / TOTAL_SUPPLY
        current_price = current_market_cap / TOTAL_SUPPLY
        
        if initial_price == 0:
            await update.message.reply_text("😿 Initial price was zero, so I can't calculate the token amount. Please check the initial market cap. Did you start before the catnip took effect?")
            return

        tokens = investment / initial_price
        current_value = tokens * current_price

        future_projections = []
        target_caps = [100_000_000, 500_000_000, 1_000_000_000]
        for target_cap in target_caps:
            target_price = target_cap / TOTAL_SUPPLY
            future_value = tokens * target_price
            future_projections.append(f"at **${target_cap:,.0f}** market cap, that's **${future_value:,.2f}**")

        keyboard = [
            [InlineKeyboardButton("🚀 Check LanLan Price Now", callback_data='check_lanlan_price')],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = (
            f"🎉 Wow! If you invested **${investment:,.2f}** at **${initial_market_cap:,.0f}** market cap, "
            f"you would have **{tokens:,.2f}** LanLan tokens.\n\n"
            f"Currently, at **${current_market_cap:,.0f}** market cap, your investment is worth **${current_value:,.2f}**.\n\n"
            f"Looking ahead, your purr-tential gains could be:\n" + "\n".join(future_projections) + "!\n\n"
            f"Get ready for a cat-tastic ride! 🚀😺"
        )
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
        
    except ValueError:
        await update.message.reply_text("That doesn't look like valid numbers. Please enter your investment and initial market cap as numbers. Example: `/lanlan 100 5000000`")
    except Exception as e:
        logger.error(f"Error in lanlan command: {e}")
        await update.message.reply_text("😿 An unexpected error occurred during calculation. The cat's puzzled! Please try again.")

async def wen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("😺 Meow meow! LanLan is ready to soar, are you? 🚀🧲 Oranga is the new Cat!")

async def whomadethebot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("@nakatroll")

async def setimage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Image settings are now hardcoded for stability. This command is currently disabled. Contact a developer if you need changes to the default or millionaire images.")

async def setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: # Removed duplicate 'Update'
    global SCHEDULED_INTERVAL, SCHEDULED_INTERVAL_STR

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if chat_type == "private":
        await update.message.reply_text("😺 This command can only be used in group chats by an administrator! 🌟")
        return

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        if not any(admin.user.id == user_id for admin in admins):
            await update.message.reply_text("😺 Sorry, only group admins can set the schedule interval! 🌟")
            return
        
        if not context.args or len(context.args) != 1:
            await update.message.reply_text("😺 Usage: `/setschedule <interval>` (e.g., `2h`, `30m`).")
            return

        new_interval_str = context.args[0]
        new_interval_seconds = parse_interval_string(new_interval_str)

        if new_interval_seconds is None or new_interval_seconds <= 0:
            await update.message.reply_text("That's not a valid interval. Please use formats like `2h` (2 hours) or `30m` (30 minutes). Meow-ch!")
            return
        
        SCHEDULED_INTERVAL = new_interval_seconds
        SCHEDULED_INTERVAL_STR = new_interval_str
        
        job_queue: JobQueue = context.application.job_queue
        current_jobs = job_queue.get_jobs_by_name("scheduled_price_update")
        for job in current_jobs:
            job.schedule_removal()
            logger.info("Removed existing scheduled job.")

        job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
        logger.info(f"Scheduled job updated to interval: {SCHEDULED_INTERVAL_STR}")
        await update.message.reply_text(f"🎉 Scheduled updates will now repeat every **{SCHEDULED_INTERVAL_STR.replace('h', ' hours').replace('m', ' minutes')}**! Cat-tastic!")

    except Exception as e:
        logger.error(f"Error in setschedule: {e}")
        await update.message.reply_text("😿 An error occurred while setting the schedule. Please try again!")

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_known_market_cap, current_investment_example_index, settings

    market_cap = fetch_market_cap()
    if market_cap is None:
        logger.warning("Scheduled job skipped due to market cap fetch failure")
        return
    
    if TOTAL_SUPPLY == 0:
        logger.warning("TOTAL_SUPPLY is zero, skipping scheduled job price calculation.")
        return

    price = market_cap / TOTAL_SUPPLY
    
    milestones = [
        10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000,
        100_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000,
        1_000_000_000, 1_500_000_000, 2_000_000_000, 5_000_000_000, 10_000_000_000
    ]

    highest_milestone_achieved = settings.get('highest_milestone_achieved', 0)
    for milestone_val in sorted(milestones):
        if market_cap >= milestone_val and milestone_val > highest_milestone_achieved:
            highest_milestone_achieved = milestone_val
            settings['highest_milestone_achieved'] = highest_milestone_achieved
            save_json(SETTINGS_FILE, settings)
            logger.info(f"Updated highest_milestone_achieved to {highest_milestone_achieved}")

    current_milestone_start_for_progress = highest_achieved
    next_milestone_end_for_progress = None
    
    for milestone_val in sorted(milestones):
        if milestone_val > highest_achieved:
            next_milestone_end_for_progress = milestone_val
            break
    
    if next_milestone_end_for_progress is None:
        if milestones:
            current_milestone_start_for_progress = milestones[-1]
            next_milestone_end_for_progress = current_milestone_start_for_progress * 1.5
        else:
            current_milestone_start_for_progress = 0
            next_milestone_end_for_progress = 10_000_000

    progress_bar = generate_progress_bar(market_cap, current_milestone_start_for_progress, next_milestone_end_for_progress)

    if last_known_market_cap is not None:
        for milestone_cap in sorted(milestones):
            if last_known_market_cap < milestone_cap <= market_cap:
                milestone_message = (
                    f"✨🎉 WoW! LanLan just crossed the **${milestone_cap:,.0f}** market cap milestone! "
                    f"Current Market Cap: **${market_cap:,.0f}** 🚀😺"
                )
                for group_id in list(groups):
                    try:
                        await context.bot.send_photo(chat_id=group_id, photo=random.choice(MILESTONE_GIF_URLS), caption=milestone_message, parse_mode='Markdown')
                        logger.info(f"Sent milestone message for ${milestone_cap:,.0f} to group {group_id}")
                    except Exception as e:
                        logger.warning(f"Failed to send milestone GIF/message to group {group_id}: {e}")
    
    last_known_market_cap = market_cap

    investment_amount_to_show = INVESTMENT_EXAMPLES[current_investment_example_index]
    current_investment_example_index = (current_investment_example_index + 1) % len(INVESTMENT_EXAMPLES)

    initial_market_cap_for_example = 5_000_000
    
    if initial_market_cap_for_example == 0:
        initial_price_for_example = 0
    else:
        initial_price_for_example = initial_market_cap_for_example / TOTAL_SUPPLY

    if initial_price_for_example == 0:
        tokens_at_initial = 0
    else:
        tokens_at_initial = investment_amount_to_show / initial_price_for_example

    current_value_at_initial_investment = tokens_at_initial * price

    tokens_now = investment_amount_to_show / price if price > 0 else 0

    future_value_messages = []
    for target_cap in [100_000_000, 500_000_000, 1_000_000_000]:
        target_price = target_cap / TOTAL_SUPPLY if TOTAL_SUPPLY > 0 else 0
        value_at_target = tokens_now * target_price if tokens_now > 0 else 0
        future_value_messages.append(f"• at **${target_cap:,.0f}** MC: **${value_at_target:,.2f}**")
            
    buy_now_message_part = (
        f"If you bought **${investment_amount_to_show:,.0f}** LanLan today, your investment could be:\n"
        "\n".join(future_value_messages)
    )

    image_url = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    
    message = (
        f"🌟 LanLan is currently purring! 😺\n"
        f"**MC:** **${market_cap:,.0f}** | **Price:** **${price:,.10f}**\n"
        f"**Next Target:** **${next_milestone_end_for_progress:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"📈 Invested **${investment_amount_to_show:,.0f}** at **${initial_market_cap_for_example:,.0f}** MC? "
        f"It's now worth **${current_value_at_initial_investment:,.2f}**!\n"
        f"{buy_now_message_part}\n\n"
        f"Orange is the new Cat! 🍊🐾"
    )

    for group_id in list(groups):
        try:
            await context.bot.send_photo(chat_id=group_id, photo=image_url, caption=message, parse_mode='Markdown')
            logger.info(f"Sent scheduled update to group {group_id}")
        except Exception as e:
            logger.warning(f"Failed to send message to group {group_id}: {e}")

# Flask app initialization
flask_app = Flask(__name__)

# Global variable to hold the PTB Application instance
ptb_application = None
# This will be initialized in the startup hook for uvicorn

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
async def telegram_webhook():
    global ptb_application

    if not ptb_application:
        logger.error("Telegram Application not initialized for webhook. Rejecting update.")
        return jsonify({"status": "error", "message": "Bot not ready"}), 503

    try:
        await ptb_application.process_update(Update.de_json(request.json, ptb_application.bot))
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error processing Telegram webhook update: {e}")
        logger.exception("Full traceback for webhook processing error:")
        return jsonify({"status": "error", "message": "Processing failed"}), 500

@flask_app.route("/")
def home():
    return "LanLan Bot is running!"

@flask_app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "message": "Bot operational"})


# --- New setup for ASGI server ---
# We define an async startup function that will be called by Uvicorn.
# This replaces the asyncio.run(main()) at the bottom of the script.
async def startup_bot():
    global last_known_market_cap, settings, groups, SCHEDULED_INTERVAL, SCHEDULED_INTERVAL_STR, ptb_application

    # --- Critical checks for required environment variables ---
    if not TELEGRAM_TOKEN:
        logger.critical("TELEGRAM_TOKEN is not set. Bot cannot function without it.")
        raise ValueError("TELEGRAM_TOKEN environment variable is required.")
    if not THEGRAPH_API_KEY:
        logger.critical("THEGRAPH_API_KEY is not set. Bot cannot function without it.")
        raise ValueError("THEGRAPH_API_KEY environment variable is required.")
    if not MAMA_COIN_ADDRESS:
        logger.critical("MAMA_COIN_ADDRESS is not set. Bot cannot function without it.")
        raise ValueError("MAMA_COIN_ADDRESS environment variable is required.")
    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL is not set. This is required for webhook mode.")
        raise ValueError("WEBHOOK_URL environment variable is required for webhook mode.")

    ptb_application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .updater(None)
        .job_queue(JobQueue())
        .build()
    )
    logger.info("Application initialized successfully for webhooks")

    await ptb_application.initialize()
    logger.info("Application initialized for async operations.")

    settings.update(load_json(SETTINGS_FILE, {"highest_milestone_achieved": 0})) # Use update for settings
    settings["default_image_url"] = DEFAULT_IMAGE_URL
    settings["scheduled_and_check_price_image_url"] = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    save_json(SETTINGS_FILE, settings)

    groups_list = load_json(GROUPS_FILE, []) 
    groups.update(groups_list) # Use update for sets

    initial_mc = fetch_market_cap()
    last_known_market_cap = initial_mc if initial_mc is not None else 0
    if initial_mc is not None:
        logger.info(f"Initial market cap fetched: ${last_known_market_cap:,.0f}")
    else:
        logger.warning("Could not fetch initial market cap. Milestone tracking might be inaccurate at start.")

    ptb_application.add_handler(CommandHandler("start", start))
    ptb_application.add_handler(CommandHandler("wen", wen))
    ptb_application.add_handler(CommandHandler("lanlan", lanlan_command))
    ptb_application.add_handler(CommandHandler("setimage", setimage))
    ptb_application.add_handler(CommandHandler("setschedule", setschedule))
    ptb_application.add_handler(CommandHandler("whomadethebot", whomadethebot))
    ptb_application.add_handler(CommandHandler("help", help_command))

    ptb_application.add_handler(CallbackQueryHandler(button_handler, pattern='^(check_lanlan_price|start_lanlan_calculation|back_to_main)$'))

    # Crucially, here we use ptb_application.start() to run the PTB application's
    # internal loops (including JobQueue) as a background task.
    # Uvicorn manages the main event loop.
    asyncio.create_task(ptb_application.start()) # Start PTB's internal event loop tasks
    logger.info("PTB Application background task started using ptb_application.start().")

    try:
        full_webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        await ptb_application.bot.set_webhook(url=full_webhook_url, allowed_updates=["message", "callback_query"])
        logger.info(f"Telegram webhook set to: {full_webhook_url}")
    except Exception as e:
        logger.error(f"Failed to set Telegram webhook: {e}")
        logger.exception("Full traceback for set_webhook error:")

    try:
        job_queue: JobQueue = ptb_application.job_queue
        if SCHEDULED_INTERVAL is not None and SCHEDULED_INTERVAL > 0:
            job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
            logger.info(f"Scheduled job set successfully with interval: {SCHEDULED_INTERVAL_STR}")
        else:
            logger.error(f"Invalid SCHEDULED_INTERVAL ({SCHEDULED_INTERVAL_STR}), scheduled job will not run.")
    except Exception as e:
        logger.error(f"Failed to schedule job: {e}")
        logger.exception("Full traceback for job scheduling error:")
    
    logger.info("Bot startup routines complete.")

# This function will be called by Uvicorn before the server starts
@flask_app.before_serving
async def setup_bot():
    await startup_bot()

# We no longer run Flask directly here, nor asyncio.run(main())
# This script will be run by `gunicorn` with a `uvicorn.workers.UvicornWorker`
# The `application` variable is what gunicorn/uvicorn expects.
application = flask_app
