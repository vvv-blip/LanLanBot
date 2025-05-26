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
    ConversationHandler # Still needed for other potential multi-step interactions if added later
)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
THEGRAPH_API_KEY = os.getenv("THEGRAPH_API_KEY", "6eddad8f39fa53f77b3364dc72aeca36")
MAMA_COIN_ADDRESS = os.getenv("MAMA_COIN_ADDRESS", "0xEccA809227d43B895754382f1fd871628d7E51FB")
try:
    TOTAL_SUPPLY = float(os.getenv("TOTAL_SUPPLY", "8888888888"))
except ValueError:
    TOTAL_SUPPLY = 8888888888.0
    logger.warning("Invalid TOTAL_SUPPLY, using default: 8888888888")

def parse_interval_string(interval_str):
    if not isinstance(interval_str, str):
        return None

    match_h = re.match(r'(\d+)\s*h', interval_str, re.IGNORECASE)
    match_m = re.match(r'(\d+)\s*m', interval_str, re.IGNORECASE)
    
    if match_h:
        return int(match_h.group(1)) * 3600
    elif match_m:
        return int(match_m.group(1)) * 60
    else:
        return None

# Constants
SETTINGS_FILE = "settings.json"
GROUPS_FILE = "groups.json"
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id/EYCKATKGBKLWvSfwvBjzfCBmGwYNdVkduYXVivCsLRFu"

SCHEDULED_INTERVAL_STR = os.getenv("SCHEDULED_INTERVAL", "2h")
SCHEDULED_INTERVAL = parse_interval_string(SCHEDULED_INTERVAL_STR)
if SCHEDULED_INTERVAL is None:
    SCHEDULED_INTERVAL = 7200
    logger.warning(f"Invalid SCHEDULED_INTERVAL format '{SCHEDULED_INTERVAL_STR}', using default: {SCHEDULED_INTERVAL} seconds.")

SCHEDULED_FIRST = 60
FALLBACK_IMAGE_URL = "https://i.imgur.com/default.jpg"
GROWTH_GIF_URLS = [
    "https://i.imgur.com/growth1.gif", # Replace with actual GIF links
    "https://i.imgur.com/growth2.gif",
    "https://i.imgur.com/growth3.gif",
    "https://i.imgur.com/growth4.gif",
    "https://i.imgur.com/growth5.gif",
]
MILESTONE_GIF_URLS = [
    "https://i.imgur.com/milestone1.gif", # Replace with actual GIF links for milestones
    "https://i.imgur.com/milestone2.gif",
    "https://i.imgur.com/milestone3.gif",
    "https://i.imgur.com/milestone4.gif",
    "https://i.imgur.com/milestone5.gif",
]

# Data persistence
def load_json(file_path, default):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load {file_path}: {e}. Using default.")
        return default

def save_json(file_path, data):
    try:
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save {file_path}: {e}")

# Initialize data
settings = load_json(SETTINGS_FILE, {
    "default_image_url": "https://i.imgur.com/placeholder1.jpg",
    "millionaire_image_url": "https://i.imgur.com/placeholder2.jpg",
    "check_price_image_url": "https://i.imgur.com/default.jpg"
})
groups = set(load_json(GROUPS_FILE, []))

# Global variable to store the last known market cap for milestone checks
last_known_market_cap = None

def generate_progress_bar(current_value, start_milestone, end_milestone, bar_length=10):
    if end_milestone <= start_milestone:
        if current_value >= end_milestone and end_milestone > 0:
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
        return None
    except json.JSONDecodeError as json_err:
        logger.error(f"JSON decode error from subgraph response: {json_err}. Response: {response.text if 'response' in locals() else 'N/A'}")
        return None
    except KeyError as key_err:
        logger.error(f"Key error in subgraph data structure: {key_err}. Data: {data if 'data' in locals() else 'N/A'}")
        return None
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
        [InlineKeyboardButton("📚 See Commands & Info", callback_data='help_menu')], # Renamed and rephrased
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎉 Hey LanLan enjoyoors! 😺 "
        "Choose an option below to get started! 🌟",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == 'check_lanlan_price':
        await query.message.reply_text("🐾 Fetching the freshest LanLan deets for you... One moment! 🐱")
        await lanlan_price_status(query, context)
    elif query.data == 'help_menu':
        await help_command(query, context)

async def lanlan_price_status(update_object: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    market_cap = fetch_market_cap()
    if market_cap is None:
        await update_object.message.reply_text("😿 Oh no, couldn’t fetch LanLan data! Please try again later.")
        return
    
    if TOTAL_SUPPLY == 0:
        await update_object.message.reply_text("😿 Total supply is zero, cannot calculate price.")
        return

    price = market_cap / TOTAL_SUPPLY

    milestones = [
        10_000_000, 20_000_000, 30_000_000, 40_000_000, 50_000_000,
        100_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000,
        1_000_000_000, 1_500_000_000, 2_000_000_000, 5_000_000_000, 10_000_000_000
    ]
    
    current_milestone_start = 0
    next_milestone_end = None
    
    for i in range(len(milestones)):
        if market_cap < milestones[i]:
            if i > 0:
                current_milestone_start = milestones[i-1]
            next_milestone_end = milestones[i]
            break
    
    if next_milestone_end is None:
        if milestones:
            current_milestone_start = milestones[-1]
            next_milestone_end = current_milestone_start * 1.5 if current_milestone_start > 0 else 10_000_000
        else:
            current_milestone_start = 0
            next_milestone_end = 10_000_000

    progress_bar = generate_progress_bar(market_cap, current_milestone_start, next_milestone_end)

    message = (
        f"🌟 LanLan is currently flying high! 😺\n"
        f"Current Market Cap: **${market_cap:,.0f}**\n"
        f"Current Price: **${price:,.10f}**\n\n"
        f"Next Target: **${next_milestone_end:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"Orange is the new Cat 🌕"
    )
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    image_to_send = settings.get("check_price_image_url", FALLBACK_IMAGE_URL)

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

async def help_command(update_object: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "😺 LanLan Bot Commands:\n"
        "/start - Welcome to the LanLan moonshot! (Shows main menu)\n"
        "/wen - Check if LanLan's ready to soar 🧲\n"
        "/lanlan <investment_usd> <initial_market_cap> - Calculate potential gains from your past investment\n" # Simplified /lanlan
        "/setimage <regular|millionaire|check_price> <url> - Set images (admins only)\n"
        "/help - Show this help message\n"
        "Scheduled updates every {}. 🚀".format(SCHEDULED_INTERVAL_STR.replace("h", " hours").replace("m", " minutes"))
    )
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data='back_to_main')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if isinstance(update_object, Update) and update_object.message:
        await update_object.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)
    elif update_object.callback_query:
        await update_object.callback_query.message.reply_text(message, parse_mode='Markdown', reply_markup=reply_markup)

async def wen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("😺 Meow meow 🧲")

# Simplified /lanlan command
async def lanlan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("😺 Usage: /lanlan <investment_in_usd> <market_cap_at_investment>")
        return
    try:
        investment = float(context.args[0])
        initial_market_cap = float(context.args[1])

        if investment <= 0 or initial_market_cap <= 0:
            await update.message.reply_text("😺 Investment and initial market cap must be positive numbers!")
            return

        current_market_cap = fetch_market_cap()
        if current_market_cap is None:
            await update.message.reply_text("😿 Oh no, couldn’t fetch LanLan data! Please try again later.")
            return
        
        if initial_market_cap == 0:
            await update.message.reply_text("😿 Your initial market cap was zero, cannot calculate potential gains.")
            return
        if TOTAL_SUPPLY == 0:
            await update.message.reply_text("😿 Total supply is zero, cannot calculate potential gains.")
            return

        initial_price = initial_market_cap / TOTAL_SUPPLY
        current_price = current_market_cap / TOTAL_SUPPLY
        
        if initial_price == 0:
            await update.message.reply_text("😿 Initial price was zero, cannot calculate token amount.")
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

        await update.message.reply_text(
            f"🎉 Wow! If you invested **${investment:,.2f}** at **${initial_market_cap:,.0f}** market cap, "
            f"you would have **{tokens:,.2f}** LanLan tokens.\n\n"
            f"Currently, at **${current_market_cap:,.0f}** market cap, your investment is worth **${current_value:,.2f}**.\n\n"
            f"Looking ahead:\n" + "\n".join(future_projections) + "!\n\n"
            f"🚀😺"
            , parse_mode='Markdown'
            , reply_markup=reply_markup
        )
    except ValueError:
        await update.message.reply_text("😺 Please enter valid numbers for investment and market cap! Usage: `/lanlan <investment_in_usd> <market_cap_at_investment>`")
    except Exception as e:
        logger.error(f"Error in lanlan command: {e}")
        await update.message.reply_text("😿 An unexpected error occurred. Please try again.")

async def setimage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 2:
        await update.message.reply_text("😺 Usage: /setimage <regular|millionaire|check_price> <url>")
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type

    if chat_type == "private":
        await update.message.reply_text("😺 This command can only be used in group chats by an administrator! 🌟")
        return

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        if not any(admin.user.id == user_id for admin in admins):
            await update.message.reply_text("😺 Sorry, only group admins can set images! 🌟")
            return
        
        image_type, url = context.args
        if image_type.lower() == "regular":
            settings["default_image_url"] = url
            await update.message.reply_text("🎉 Regular image updated! 😺")
        elif image_type.lower() == "millionaire":
            settings["millionaire_image_url"] = url
            await update.message.reply_text("🎉 Millionaire image updated! 😺")
        elif image_type.lower() == "check_price":
            settings["check_price_image_url"] = url
            await update.message.reply_text("🎉 'Check LanLan Price' image updated! 😺")
        else:
            await update.message.reply_text("😺 Use 'regular', 'millionaire', or 'check_price' for image type!")
        save_json(SETTINGS_FILE, settings)
    except Exception as e:
        logger.error(f"Error in setimage: {e}")
        await update.message.reply_text("😿 Error setting image. Please try again!")

async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_known_market_cap

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

    current_milestone_start = 0
    next_milestone_end = None
    
    for i in range(len(milestones)):
        if market_cap < milestones[i]:
            if i > 0:
                current_milestone_start = milestones[i-1]
            next_milestone_end = milestones[i]
            break
    
    if next_milestone_end is None:
        if milestones:
            current_milestone_start = milestones[-1]
            next_milestone_end = current_milestone_start * 1.5 if current_milestone_start > 0 else 10_000_000
        else:
            current_milestone_start = 0
            next_milestone_end = 10_000_000

    progress_bar = generate_progress_bar(market_cap, current_milestone_start, next_milestone_end)

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

    investment_example_1 = 100
    initial_market_cap_for_example = 5_000_000 
    
    if initial_market_cap_for_example == 0:
        initial_price_for_example = 0
    else:
        initial_price_for_example = initial_market_cap_for_example / TOTAL_SUPPLY

    if initial_price_for_example == 0:
        tokens_at_initial = 0
    else:
        tokens_at_initial = investment_example_1 / initial_price_for_example

    current_value_at_initial_investment = tokens_at_initial * price

    investment_now = 100
    tokens_now = investment_now / price if price > 0 else 0

    future_value_messages = []
    for target_cap in [100_000_000, 500_000_000, 1_000_000_000]:
        target_price = target_cap / TOTAL_SUPPLY if TOTAL_SUPPLY > 0 else 0
        value_at_target = tokens_now * target_price if tokens_now > 0 else 0
        future_value_messages.append(f"at **${target_cap:,.0f}** it would be **${value_at_target:,.2f}**")
    
    buy_now_message_part = ""
    if future_value_messages:
        buy_now_message_part = (
            f"🚀 And if you bought **${investment_now:,.0f}** LanLan today, "
            f"your investment would be worth:\n"
            f"{'!\n'.join(future_value_messages)}!"
        )

    image_url = settings.get(
        "millionaire_image_url" if current_value_at_initial_investment > 1_000_000 else "default_image_url",
        FALLBACK_IMAGE_URL
    )
    
    message = (
        f"🌟 LanLan dreams are soaring! 😺\n"
        f"Current Market Cap: **${market_cap:,.0f}**\n"
        f"Current Price: **${price:,.10f}**\n\n"
        f"Next Target: **${next_milestone_end:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"📈 If you invested **${investment_example_1:,.0f}** at a **${initial_market_cap_for_example:,.0f}** market cap, "
        f"you’d now have **${current_value_at_initial_investment:,.2f}**!\n\n"
        f"{buy_now_message_part}\n\n"
        f"Orange is the new Cat! 🌕"
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


def main():
    global last_known_market_cap

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is not set")
        raise ValueError("TELEGRAM_TOKEN environment variable is required")

    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).job_queue(JobQueue()).build()
        logger.info("Application initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize application: {e}")
        raise

    initial_mc = fetch_market_cap()
    last_known_market_cap = initial_mc if initial_mc is not None else 0
    if initial_mc is not None:
        logger.info(f"Initial market cap fetched: ${last_known_market_cap:,.0f}")
    else:
        logger.warning("Could not fetch initial market cap. Milestone tracking might be inaccurate at start.")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wen", wen))
    app.add_handler(CommandHandler("lanlan", lanlan)) # Re-added as simple command handler
    app.add_handler(CommandHandler("setimage", setimage))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(button_handler, pattern='^(check_lanlan_price|help_menu)$'))
    app.add_handler(CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main$'))

    try:
        job_queue: JobQueue = app.job_queue
        if SCHEDULED_INTERVAL is not None and SCHEDULED_INTERVAL > 0:
            job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST)
            logger.info(f"Scheduled job set successfully with interval: {SCHEDULED_INTERVAL_STR}")
        else:
            logger.error(f"Invalid SCHEDULED_INTERVAL ({SCHEDULED_INTERVAL_STR}), job not scheduled.")
            raise ValueError("Scheduled interval is invalid.")
    except Exception as e:
        logger.error(f"Failed to schedule job: {e}")
        raise

    try:
        logger.info("Bot started")
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Bot polling failed: {e}")
        raise

if __name__ == "__main__":
    main()
