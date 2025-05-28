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
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    JobQueue,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler
)
from telegram.ext._application import Application


# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY", "6eddad8f39fa53f77b33644dc72aeca36") # Using a default key for demonstration
MAMA_COIN_ADDRESS = os.getenv("MAMA_COIN_ADDRESS", "0xEccA809227d43B895754382f1fd871628d7E51FB")
try:
    TOTAL_SUPPLY = float(os.getenv("TOTAL_SUPPLY", "8888888888"))
except ValueError:
    TOTAL_SUPPLY = 8888888888.0
    logger.warning("Invalid TOTAL_SUPPLY, using default: 8888888888")

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
# Using the provided subgraph ID
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id/A3Np3RQbaBA6oKJgiwDJeo5T3zrYfGHPWFYayMwtNDum"

# Load SCHEDULED_INTERVAL from environment or default (will be overwritten by settings.json later)
SCHEDULED_INTERVAL_STR = os.getenv("SCHEDULED_INTERVAL", "2h")
SCHEDULED_INTERVAL = parse_interval_string(SCHEDULED_INTERVAL_STR)
if SCHEDULED_INTERVAL is None:
    SCHEDULED_INTERVAL = 7200
    logger.warning(f"Invalid SCHEDULED_INTERVAL format '{SCHEDULED_INTERVAL_STR}', using default: {SCHEDULED_INTERVAL} seconds.")

SCHEDULED_FIRST = 60 # For the main price update

# --- UPDATED IMAGE URLs ---
DEFAULT_IMAGE_URL = "https://i.imgur.com/LFE9ouI.jpeg"
SCHEDULED_AND_CHECK_PRICE_IMAGE_URL = "https://i.imgur.com/EkpFRCD.jpeg"

GROWTH_GIF_URLS = [
    "https://i.imgur.com/KxzLvcu.gif",
    "https://i.imgur.com/K4uOUd3.gif",
    "https://i.imgur.com/PW3v7q9.gif",
    "https://i.imgur.com/EJ5GzcK.gif",
    "https://i.imgur.com/btixIWt.gif",
]
MILESTONE_GIF_URLS = [
    "https://i.imgur.com/KxzLvcu.gif",
    "https://i.imgur.com/K4uOUd3.gif",
    "https://i.imgur.com/PW3v7q9.gif",
    "https://i.imgur.com/EJ5GzcK.gif",
    "https://i.imgur.com/btixIWt.gif",
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

# Initialize data (loaded in setup_application function for proper scope)
settings = {}
groups = set()

# Global variable to store the last known market cap for milestone checks
last_known_market_cap = None
# Global variable to remember which investment example was last shown in scheduled messages
current_investment_example_index = 0
INVESTMENT_EXAMPLES = [100, 1000, 10000]

# Helper to generate progress bar
def generate_progress_bar(current_value, start_milestone, end_milestone, bar_length=10):
    if end_milestone <= start_milestone:
        if current_value >= end_milestone:
            return "[██████████] 100%"
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
    return f"[{bar}] {progress_percentage:.0f}%"

# Fetch LanLan market cap from Uniswap V2
def fetch_market_cap():
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
        logger.info(f"Fetching market cap for token ID: {MAMA_COIN_ADDRESS.lower()} from {SUBGRAPH_URL}")
        response = requests.post(SUBGRAPH_URL, json={"query": query}, timeout=15)
        response.raise_for_status()
        
        json_response = response.json()
        
        if "errors" in json_response:
            logger.error(f"Subgraph returned errors: {json_response['errors']}")
            return None

        data = json_response.get("data")
        if not data:
            logger.error(f"Subgraph response missing 'data' field. Response: {json_response}")
            return None

        token_data = data.get("token")
        if not token_data:
            logger.error(f"No token data found for LanLan token with ID: {MAMA_COIN_ADDRESS.lower()} in subgraph data. Data: {data}")
            return None
            
        bundle_data = data.get("bundle")
        if not bundle_data or "ethPrice" not in bundle_data:
            logger.error(f"No bundle data or ethPrice found in subgraph data. Data: {data}")
            return None

        eth_price_usd = float(bundle_data["ethPrice"])
        token_price_eth = float(token_data["derivedETH"])

        token_price_usd = token_price_eth * eth_price_usd
        market_cap = token_price_usd * TOTAL_SUPPLY
        logger.info(f"Fetched market cap: ${market_cap:,.0f}")
        return market_cap
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Network or HTTP error fetching market cap: {req_err}")
        return None
    except json.JSONDecodeError as json_err:
        logger.error(f"JSON decode error from subgraph response: {json_err}. Response: {response.text if 'response' in locals() else 'N/A'}")
    except KeyError as key_err:
        logger.error(f"Key error in subgraph data structure: {key_err}. This typically means a field was missing. Data: {data if 'data' in locals() else 'N/A'}")
    except Exception as e:
        logger.error(f"An unexpected error occurred fetching market cap: {e}")
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
        caption=(
            "🎉 Hey, LanLan lovers! 😺 I’m your bubbly bot tracking LanLan’s purr-gress! "
            "Choose an option below to get started. 🌟"
        ),
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with the list of commands."""
    help_text = (
        "🐾 Here are the commands you can use with LanLan Bot:\n\n"
        "• `/start`: Get started and see the main menu.\n\n"
        "• `/lanlan <investment> <initial_market_cap>`: Calculate your potential gains.\n"
        "  Example: `/lanlan 100 5000000` (meaning $100 invested at $5,000,000 market cap).\n\n"
        "• `/lanlan x <amount_bought_now> x <target_market_cap>`: Calculate future value of buying now.\n"
        "  Example: `/lanlan x 100 x 100000000` (meaning $100 bought now and calculating its value at $100,000,000 target market cap).\n\n"
        "• `/wen`: A fun check on LanLan's readiness for takeoff!\n\n"
        "• `/whomadethebot`: Find out who crafted this purr-fect bot.\n\n"
        "*Admin Commands (Group Admins Only):*\n"
        "• `/setschedule <interval>`: Set how often *main* scheduled updates are sent.\n"
        "  Example: `/setschedule 1h` or `/setschedule 30m`\n\n"
        "• `/setschedule2 <interval>`: Set how often *random investment* scheduled updates are sent.\n"
        "  Example: `/setschedule2 4h` or `/setschedule2 60m`\n\n"
        "Remember, Oranga is the new Cat! 🍊🐾"
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
            "Ready to crunch some numbers?!\n\n"
            "Just type `/lanlan <amount_invested> <initial_market_cap>`\n"
            "For example: `/lanlan 100 5000000` (meaning $100 invested at $5,000,000 market cap).\n\n"
            "Or, to calculate the future value of a *new* investment:\n"
            "`/lanlan x <amount_bought_now> x <target_market_cap>`\n"
            "For example: `/lanlan x 100 x 100000000` (meaning $100 bought now and calculating its value at $100,000,000 target market cap). Easy peasy, lemon squeezy! 🍋"
        )

async def lanlan_price_status(update_object: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    market_cap = fetch_market_cap()
    if market_cap is None:
        await update_object.message.reply_text("😿 Oh no, I couldn’t fetch LanLan data! Please try again later. The cat's on a coffee break!")
        return
    if TOTAL_SUPPLY == 0:
        await update_object.message.reply_text("😿 Total supply is zero, so I can't calculate the price. Meow-ch!")
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
        f"🌟* LanLan is currently purring!* 😺\n\n"
        f"*MC:* ${market_cap:,.0f} | *Price:* ${price:,.10f}\n"
        f"*Next Target:* ${next_milestone_end_for_progress:,.0f}\n"
        f"Progress: {progress_bar}\n\n"
        f"Orange is the new Cat! 🍊🐾"
    )
    keyboard = [
        [InlineKeyboardButton("🤔 Calculate My Investment", callback_data='start_lanlan_calculation')],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    image_to_send = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL

    try:
        await update_object.message.reply_photo(
            photo=image_to_send,
            caption=message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"Could not send image for check price status, sending text only: {e}")
        await update_object.message.reply_text(
            message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

async def lanlan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Helper for invalid usage
    async def send_lanlan_usage(chat_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Meow! To calculate your investment, please use one of these formats:\n\n"
                "1. *Initial Investment Calculation:*\n"
                "   `/lanlan <amount_invested> <initial_market_cap>`\n"
                "   Example: `/lanlan 100 5000000` (meaning $100 invested at $5,000,000 market cap).\n\n"
                "2. *Future Buy Calculation:*\n"
                "   `/lanlan x <amount_bought_now> x <target_market_cap>`\n"
                "   Example: `/lanlan x 100 x 100000000` (meaning $100 bought now and calculating its value at $100,000,000 target market cap)."
            ),
            parse_mode='Markdown'
        )

    chat_id = update.effective_chat.id

    # --- NEW FEATURE: /lanlan x <amount_bought_now> x <target_market_cap> ---
    if len(context.args) == 4 and context.args[0] == 'x' and context.args[2] == 'x':
        try:
            amount_bought_now = float(context.args[1])
            target_market_cap_new = float(context.args[3])

            if amount_bought_now <= 0 or target_market_cap_new <= 0:
                await update.message.reply_text("Please enter positive numbers for amount bought and target market cap. Purr-fect numbers, please!")
                return

            current_market_cap = fetch_market_cap()
            if current_market_cap is None:
                await update.message.reply_text("😿 Oh no, I couldn’t fetch current LanLan data! Please try again later. The cat's on a coffee break!")
                return
            if TOTAL_SUPPLY == 0:
                await update.message.reply_text("😿 Total supply is zero, so I can't calculate prices. Meow-ch!")
                return

            current_price = current_market_cap / TOTAL_SUPPLY
            if current_price == 0:
                 await update.message.reply_text("😿 Current price is zero, so I can't calculate tokens bought. Please check the current market cap. Did you buy before the catnip took effect?")
                 return

            tokens_bought = amount_bought_now / current_price
            target_price = target_market_cap_new / TOTAL_SUPPLY
            future_value_if_bought_now = tokens_bought * target_price

            message = (
                f"🎉 *Future Buy Calculation:*\n\n"
                f"If you bought *${amount_bought_now:,.2f}* worth of LanLan *now* (at current market cap: ${current_market_cap:,.0f}),\n"
                f"you would have {tokens_bought:,.2f} LanLan tokens.\n\n"
                f"At a target market cap of ${target_market_cap_new:,.0f}, your investment would be worth an estimated *${future_value_if_bought_now:,.2f}*!\n\n"
                f"Get ready for a cat-tastic ride! 🚀😺"
            )
            await update.message.reply_text(message, parse_mode='Markdown')
            return

        except ValueError:
            await update.message.reply_text("That doesn't look like valid numbers. Please enter numbers for amount bought and target market cap. Example: `/lanlan x 100 x 100000000`")
            return
        except Exception as e:
            logger.error(f"Error in new lanlan 'buy now' command: {e}")
            await update.message.reply_text("😿 An unexpected error occurred during calculation. The cat's puzzled! Please try again.")
            return

    # --- EXISTING FEATURE: /lanlan <investment> <initial_market_cap> ---
    elif len(context.args) == 2:
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
                future_projections.append(f"• at ${target_cap:,.0f} MC: ${future_value:,.2f}")

            keyboard = [
                [InlineKeyboardButton("🚀 Check LanLan Price Now", callback_data='check_lanlan_price')],
                [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            message = (
                f"🎉 *Initial Investment Calculation:*\n\n"
                f"📈 *Invested ${investment:,.2f} at ${initial_market_cap:,.0f} MC?* It's now worth *${current_value:,.2f}*!\n\n"
                f"You would have {tokens:,.2f} LanLan tokens.\n\n"
                f"Looking ahead, your purr-tential gains could be:\n" + "\n".join(future_projections) + "\n\n"
                f"Get ready for a cat-tastic ride! 🚀😺"
            )
            await update.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
            return

        except ValueError:
            await update.message.reply_text("That doesn't look like valid numbers. Please enter your investment and initial market cap as numbers. Example: `/lanlan 100 5000000`")
            return
        except Exception as e:
            logger.error(f"Error in lanlan command: {e}")
            await update.message.reply_text("😿 An unexpected error occurred during calculation. The cat's puzzled! Please try again.")
            return

    # --- INVALID USAGE ---
    else:
        await send_lanlan_usage(chat_id)


async def wen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("😺 Meow meow🧲 Orange is the new Cat!")

async def whomadethebot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("@nakatroll")

async def setimage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Image settings are now hardcoded for stability. This command is currently disabled. Contact a developer if you need changes to the default or millionaire images.")

async def setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global SCHEDULED_INTERVAL, SCHEDULED_INTERVAL_STR, settings

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
        
        # Update global and persistent settings
        SCHEDULED_INTERVAL = new_interval_seconds
        SCHEDULED_INTERVAL_STR = new_interval_str
        settings['scheduled_interval_seconds'] = SCHEDULED_INTERVAL
        settings['scheduled_interval_str'] = SCHEDULED_INTERVAL_STR
        save_json(SETTINGS_FILE, settings)
        
        job_queue: JobQueue = context.application.job_queue
        current_jobs = job_queue.get_jobs_by_name("scheduled_price_update")
        for job in current_jobs:
            job.schedule_removal()
        logger.info("Removed existing scheduled_price_update job.")

        job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
        logger.info(f"Scheduled price update job updated to interval: {SCHEDULED_INTERVAL_STR}")
        await update.message.reply_text(f"🎉 *Scheduled updates will now repeat every {SCHEDULED_INTERVAL_STR.replace('h', ' hours').replace('m', ' minutes')}!* Cat-tastic!")

    except Exception as e:
        logger.error(f"Error in setschedule: {e}")
        await update.message.reply_text("😿 An error occurred while setting the schedule. Please try again!")

# --- NEW: Random Buy Now Scheduled Job ---
async def random_buy_now_scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a scheduled message calculating a random 'buy now' scenario."""
    
    current_market_cap = fetch_market_cap()
    if current_market_cap is None:
        logger.warning("Random buy now job skipped due to market cap fetch failure.")
        return
    if TOTAL_SUPPLY == 0:
        logger.warning("TOTAL_SUPPLY is zero, skipping random buy now job price calculation.")
        return

    price = current_market_cap / TOTAL_SUPPLY
    if price == 0:
        logger.warning("Current price is zero, skipping random buy now job.")
        return

    random_investment_amount = random.randint(100, 10000) # Random amount between 100 and 10000
    target_market_cap_500m = 500_000_000.0
    target_market_cap_1b = 1_000_000_000.0

    tokens_bought = random_investment_amount / price
    
    future_value_500m = tokens_bought * (target_market_cap_500m / TOTAL_SUPPLY)
    future_value_1b = tokens_bought * (target_market_cap_1b / TOTAL_SUPPLY)

    message = (
        
    f"**Random Scenario!** 😺\n\n"
    f"If you **Buy** ${random_investment_amount:,.0f} now:\n"
    f"**Current MC**: ${current_market_cap:,.0f}\n\n"
    f"Your gains could be:\n"
    f"At **$500M MC**: ${future_value_500m:,.0f}\n"
    f"At **$1B MC**: ${future_value_1b:,.0f}\n\n"
    f"Just sayin’ meow! 🚀"
)
    
    for group_id in list(groups):
        try:
            await context.bot.send_message(chat_id=group_id, text=message, parse_mode='Markdown')
            logger.info(f"Sent random buy now message to group {group_id}")
        except Exception as e:
            logger.warning(f"Failed to send random buy now message to group {group_id}: {e}")

# --- NEW: setschedule2 command handler ---
async def setschedule2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global settings # Access global settings to save persistent intervals

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
            await update.message.reply_text("😺 Usage: `/setschedule2 <interval>` (e.g., `4h`, `15m`).")
            return

        new_interval_str = context.args[0]
        new_interval_seconds = parse_interval_string(new_interval_str)

        if new_interval_seconds is None or new_interval_seconds <= 0:
            await update.message.reply_text("That's not a valid interval. Please use formats like `4h` (4 hours) or `15m` (15 minutes). Meow-ch!")
            return
        
        # Update persistent settings for this specific job
        settings['random_buy_now_interval_seconds'] = new_interval_seconds
        settings['random_buy_now_interval_str'] = new_interval_str
        save_json(SETTINGS_FILE, settings)
        
        job_queue: JobQueue = context.application.job_queue
        # Remove existing "random_buy_now_job" if it exists
        current_jobs = job_queue.get_jobs_by_name("random_buy_now_job")
        for job in current_jobs:
            job.schedule_removal()
        logger.info("Removed existing random_buy_now_job.")

        # Add the new job with the updated interval
        # Set a small 'first' value, so it starts soon after being set
        job_queue.run_repeating(random_buy_now_scheduled_job, interval=new_interval_seconds, first=60, name="random_buy_now_job")
        logger.info(f"Random buy now job updated to interval: {new_interval_str}")
        await update.message.reply_text(f"🎉 *Random buy now updates will now repeat every {new_interval_str.replace('h', ' hours').replace('m', ' minutes')}!* Get ready for new scenarios!")

    except Exception as e:
        logger.error(f"Error in setschedule2: {e}")
        await update.message.reply_text("😿 An error occurred while setting the schedule. Please try again!")

# --- END OF NEW COMMAND AND JOB ---


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

    current_milestone_start_for_progress = highest_milestone_achieved
    next_milestone_end_for_progress = None
    for milestone_val in sorted(milestones):
        if milestone_val > highest_milestone_achieved:
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

    if last_known_market_cap is not None:
        for milestone_cap in sorted(milestones):
            if last_known_market_cap < milestone_cap <= market_cap:
                milestone_message = (
                    f"✨🎉 *WoW! LanLan just crossed the ${milestone_cap:,.0f} market cap milestone!* "
                    f"Current Market Cap: ${market_cap:,.0f} 🚀😺"
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

    tokens_now_example = investment_amount_to_show / price if price > 0 else 0

    future_value_messages = []
    for target_cap in [100_000_000, 500_000_000, 1_000_000_000]:
        target_price = target_cap / TOTAL_SUPPLY if TOTAL_SUPPLY > 0 else 0
        value_at_target = tokens_now_example * target_price if tokens_now_example > 0 else 0
        future_value_messages.append(f"• at ${target_cap:,.0f} MC: ${value_at_target:,.2f}")


    image_url = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    message = (
        f"🌟* LanLan is currently purring!* 😺\n\n"
        f"*MC:* ${market_cap:,.0f} | *Price:* ${price:,.10f}\n"
        f"*Next Target:* ${next_milestone_end_for_progress:,.0f}\n"
        f"Progress: {progress_bar}\n\n"
        f"📈 *Invested ${investment_amount_to_show:,.0f} at ${initial_market_cap_for_example:,.0f} MC?* It's now worth ${current_value_at_initial_investment:,.2f}!\n\n"
        f"If you bought *${investment_amount_to_show:,.0f}* LanLan today, your investment could be:\n"
        + "\n".join(future_value_messages) + "\n\n"
        f"Orange is the new Cat! 🍊🐾"
    )

    for group_id in list(groups):
        try:
            await context.bot.send_photo(chat_id=group_id, photo=image_url, caption=message, parse_mode='Markdown')
            logger.info(f"Sent scheduled update to group {group_id}")
        except Exception as e:
            logger.warning(f"Failed to send message to group {group_id}: {e}")
async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

    dummy_update = Update(update_id=update.update_id)
    dummy_update._effective_chat = query.message.chat
    dummy_update._effective_message = query.message

    await start(dummy_update, context)


# Refactored main() into an async setup function
async def setup_application() -> Application:
    global last_known_market_cap, settings, groups, SCHEDULED_INTERVAL, SCHEDULED_INTERVAL_STR

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is not set")
        raise ValueError("TELEGRAM_TOKEN environment variable is required")

    try:
        # Build the Application instance
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).job_queue(JobQueue()).build()
        logger.info("Application initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize application: {e}")
        raise

    # Initialize data (settings and groups)
    settings = load_json(SETTINGS_FILE, {
        "highest_milestone_achieved": 0,
        "scheduled_interval_seconds": SCHEDULED_INTERVAL, # Ensure default is saved
        "scheduled_interval_str": SCHEDULED_INTERVAL_STR, # Ensure default is saved
        "random_buy_now_interval_seconds": 14400, # Default to 4 hours for new job (4h * 3600s/h)
        "random_buy_now_interval_str": "4h", # Default for new job
    })

    # Override defaults with loaded settings if they exist
    SCHEDULED_INTERVAL = settings.get('scheduled_interval_seconds', SCHEDULED_INTERVAL)
    SCHEDULED_INTERVAL_STR = settings.get('scheduled_interval_str', SCHEDULED_INTERVAL_STR)
    RANDOM_BUY_NOW_INTERVAL_SECONDS = settings.get('random_buy_now_interval_seconds', 14400) # Load for new job
    RANDOM_BUY_NOW_INTERVAL_STR = settings.get('random_buy_now_interval_str', "4h") # Load for new job


    settings["default_image_url"] = DEFAULT_IMAGE_URL
    settings["scheduled_and_check_price_image_url"] = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    save_json(SETTINGS_FILE, settings) # Save back the potentially updated settings

    groups_list = load_json(GROUPS_FILE, [])
    groups = set(groups_list)

    initial_mc = fetch_market_cap()
    last_known_market_cap = initial_mc if initial_mc is not None else 0
    if initial_mc is not None:
        logger.info(f"Initial market cap fetched: ${last_known_market_cap:,.0f}")
    else:
        logger.warning("Could not fetch initial market cap. Milestone tracking might be inaccurate at start.")

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wen", wen))
    app.add_handler(CommandHandler("lanlan", lanlan_command))
    app.add_handler(CommandHandler("setimage", setimage))
    app.add_handler(CommandHandler("setschedule", setschedule))
    app.add_handler(CommandHandler("setschedule2", setschedule2)) # Register new command
    app.add_handler(CommandHandler("whomadethebot", whomadethebot))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(button_handler, pattern='^(check_lanlan_price|start_lanlan_calculation)$'))
    app.add_handler(CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main$'))

    # Schedule recurring jobs
    try:
        job_queue: JobQueue = app.job_queue
        
        # Existing scheduled_job (price update)
        if SCHEDULED_INTERVAL is not None and SCHEDULED_INTERVAL > 0:
            job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
            logger.info(f"Scheduled price update job set successfully with interval: {SCHEDULEED_INTERVAL_STR}")
        else:
            logger.error(f"Invalid SCHEDULED_INTERVAL ({SCHEDULED_INTERVAL_STR}), price update job not scheduled.")
            # Do not raise ValueError here if only one job fails, let others potentially run.

        # New random buy now job
        if RANDOM_BUY_NOW_INTERVAL_SECONDS is not None and RANDOM_BUY_NOW_INTERVAL_SECONDS > 0:
            # Set 'first' run slightly after other initial jobs to avoid contention at startup
            job_queue.run_repeating(random_buy_now_scheduled_job, interval=RANDOM_BUY_NOW_INTERVAL_SECONDS, first=SCHEDULED_FIRST + 120, name="random_buy_now_job")
            logger.info(f"Random buy now job set successfully with interval: {RANDOM_BUY_NOW_INTERVAL_STR}")
        else:
            logger.error(f"Invalid RANDOM_BUY_NOW_INTERVAL ({RANDOM_BUY_NOW_INTERVAL_STR}), random buy now job not scheduled.")
            
    except Exception as e:
        logger.error(f"Failed to schedule jobs: {e}")
        # Consider whether you want to raise an exception here or just log

    logger.info("Application setup complete. Ready for webhooks.")
    return app
