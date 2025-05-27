import os
import json
import logging
import requests
import asyncio
import random
import re # For parsing time strings (e.g., "1h", "30m")
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
    ConversationHandler # Still imported but not used for /lanlan
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

# Function to parse interval strings (e.g., "1h", "30m")
def parse_interval_string(interval_str):
    if not isinstance(interval_str, str):
        return None # Return None if not a string, will use default

    match_h = re.match(r'(\d+)\s*h', interval_str)
    match_m = re.match(r'(\d+)\s*m', interval_str)
    
    if match_h:
        return int(match_h.group(1)) * 3600 # hours to seconds
    elif match_m:
        return int(match_m.group(1)) * 60 # minutes to seconds
    else:
        return None # Invalid format

# Constants
SETTINGS_FILE = "settings.json" 
GROUPS_FILE = "groups.json"
SUBGRAPH_URL = f"https://gateway.thegraph.com/api/{THEGRAPH_API_KEY}/subgraphs/id/EYCKATKGBKLWvSfwvBjzfCBmGwYNdVkduYXVivCsLRFu"

# Load SCHEDULED_INTERVAL from environment or default
SCHEDULED_INTERVAL_STR = os.getenv("SCHEDULED_INTERVAL", "2h") # Default to 2 hours
SCHEDULED_INTERVAL = parse_interval_string(SCHEDULED_INTERVAL_STR)
if SCHEDULED_INTERVAL is None:
    SCHEDULED_INTERVAL = 7200 # Fallback to 2 hours if parsing fails
    logger.warning(f"Invalid SCHEDULED_INTERVAL format '{SCHEDULED_INTERVAL_STR}', using default: {SCHEDULED_INTERVAL} seconds.")

SCHEDULED_FIRST = 60

# --- UPDATED IMAGE URLs ---
# !!! IMPORTANT: Replace these with ACTUAL direct image links (e.g., ending in .jpg, .gif, .png) !!!
# You need to go to your Imgur album, open the image, right-click, and "Copy Image Address"
DEFAULT_IMAGE_URL = "https://i.imgur.com/example_default_image.jpeg" # Placeholder for https://imgur.com/a/whegQcv
SCHEDULED_AND_CHECK_PRICE_IMAGE_URL = "https://i.imgur.com/example_millionaire_image.gif" # Placeholder for https://imgur.com/a/FVtSdd9


# Placeholder GIFs - ideally, these would also be hosted on imgur or similar
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
            # Basic type check to prevent common errors
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
        return data

def save_json(file_path, data):
    try:
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save {file_path}: {e}")

# Initialize data (loaded in main function for proper scope)
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

        # CORRECTED LINE: Used eth_price_usd instead of undefined eth_price_eth
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
        logger.error(f"Key error in subgraph data structure: {key_err}. Data: {data if 'data' in locals() else 'N/A'}")
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
        photo=DEFAULT_IMAGE_URL, # Using the default image for /start
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
        "• `/start`: Get started and see the main menu.\n"
        "• `/lanlan <investment> <initial_market_cap>`: Calculate your potential gains. "
        "Example: `/lanlan 100 5000000` (meaning **$100** invested at **$5,000,000** market cap).\n"
        "• `/wen`: A fun check on LanLan's readiness for takeoff!\n"
        "• `/whomadethebot`: Find out who crafted this purr-fect bot.\n\n"
        "**Admin Commands (Group Admins Only):**\n"
        "• `/setschedule <interval>`: Set how often scheduled updates are sent. "
        "Example: `/setschedule 1h` or `/setschedule 30m`\n\n"
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
            "Ready to crunch some numbers?!\n"
            "Just type `/lanlan <amount_invested> <initial_market_cap>`\n"
            "For example: `/lanlan 100 5000000` (meaning **$100** invested at **$5,000,000** market cap). Easy peasy, lemon squeezy! 🍋"
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
    
    # Determine the current range for the progress bar based on the highest milestone achieved
    highest_achieved = settings.get('highest_milestone_achieved', 0)
    
    current_milestone_start_for_progress = highest_achieved
    next_milestone_end_for_progress = None

    # Find the next milestone *above* the highest achieved, or the current market cap if it's higher
    for milestone_val in sorted(milestones):
        if milestone_val > highest_achieved:
            next_milestone_end_for_progress = milestone_val
            break
    
    # If current market cap is above all hardcoded milestones, set a dynamic next target
    if next_milestone_end_for_progress is None:
        if milestones:
            current_milestone_start_for_progress = milestones[-1]
            next_milestone_end_for_progress = current_milestone_start_for_progress * 1.5
        else:
            current_milestone_start_for_progress = 0
            next_milestone_end_for_progress = 10_000_000 # Fallback if no milestones at all

    # Adjust current_milestone_start_for_progress if market_cap is below the highest achieved
    # This ensures the progress bar starts from the last *relevant* point
    if market_cap < current_milestone_start_for_progress:
        # Find the milestone just below the current market cap, or 0
        temp_start = 0
        for m in sorted(milestones):
            if m <= market_cap:
                temp_start = m
            else:
                break
        current_milestone_start_for_progress = temp_start
        # Also ensure next_milestone_end_for_progress is still the correct next one
        for m in sorted(milestones):
            if m > market_cap:
                next_milestone_end_for_progress = m
                break
        if next_milestone_end_for_progress is None: # If market_cap is above all hardcoded
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

    # --- UPDATED IMAGE USAGE ---
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

# Command handler for /lanlan
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
    # This command is largely deprecated now that images are hardcoded.
    # It will only affect images specified here, not the hardcoded ones.
    await update.message.reply_text("Image settings are now hardcoded for stability. This command is currently disabled. Contact a developer if you need changes to the default or millionaire images.")

async def setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        
        # Reschedule the job
        job_queue: JobQueue = context.application.job_queue
        # Remove existing job if any
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

    # Update highest_milestone_achieved
    highest_milestone_achieved = settings.get('highest_milestone_achieved', 0)
    for milestone_val in sorted(milestones):
        if market_cap >= milestone_val and milestone_val > highest_milestone_achieved:
            highest_milestone_achieved = milestone_val
            settings['highest_milestone_achieved'] = highest_milestone_achieved
            save_json(SETTINGS_FILE, settings) # Save the updated milestone to persistence
            logger.info(f"Updated highest_milestone_achieved to {highest_milestone_achieved}")

    # Determine next target for progress bar based on highest_milestone_achieved
    current_milestone_start_for_progress = highest_milestone_achieved
    next_milestone_end_for_progress = None
    
    for milestone_val in sorted(milestones):
        if milestone_val > highest_milestone_achieved:
            next_milestone_end_for_progress = milestone_val
            break
    
    if next_milestone_end_for_progress is None:
        if milestones:
            current_milestone_start_for_progress = milestones[-1]
            next_milestone_end_for_progress = current_milestone_start_for_progress * 1.5 # Dynamic next target if past all
        else:
            current_milestone_start_for_progress = 0
            next_milestone_end_for_progress = 10_000_000 # Fallback if no milestones at all

    progress_bar = generate_progress_bar(market_cap, current_milestone_start_for_progress, next_milestone_end_for_progress)

    # Check for milestone achievements and send GIF
    if last_known_market_cap is not None:
        for milestone_cap in sorted(milestones):
            # Only trigger if we *crossed* the milestone since last check
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
    
    last_known_market_cap = market_cap # Update for next check

    # Rotate investment examples
    investment_amount_to_show = INVESTMENT_EXAMPLES[current_investment_example_index]
    current_investment_example_index = (current_investment_example_index + 1) % len(INVESTMENT_EXAMPLES)

    initial_market_cap_for_example = 5_000_000 # Example fixed initial market cap
    
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
    # Project to 100M, 500M, 1B market caps
    for target_cap in [100_000_000, 500_000_000, 1_000_000_000]:
        target_price = target_cap / TOTAL_SUPPLY if TOTAL_SUPPLY > 0 else 0
        value_at_target = tokens_now * target_price if tokens_now > 0 else 0
        future_value_messages.append(f"• at **${target_cap:,.0f}** MC: **${value_at_target:,.2f}**")
            
    buy_now_message_part = (
        f"If you bought **${investment_amount_to_show:,.0f}** LanLan today, your investment could be:\n"
        f"{'\n'.join(future_value_messages)}"
    )

    # --- UPDATED IMAGE USAGE ---
    image_url = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    
    message = (
        f"🌟 LanLan is currently purring! 😺\n"
        f"**MC:** **${market_cap:,.0f}** | **Price:** **${price:,.10f}**\n"
        f"**Next Target:** **${next_milestone_end_for_progress:,.0f}**\n"
        f"Progress: {progress_bar}\n\n"
        f"📈 Invested **${investment_amount_to_show:,.0f}** at **${initial_market_cap_for_example:,.0f}** MC? "
        f"It's now worth **${current_value_at_initial_investment:,.2f}**!\n"
        f"{buy_now_message_part}\n\n"
        f"Oranga is the new Cat! 🍊🐾"
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
    global last_known_market_cap, settings, groups, SCHEDULED_INTERVAL, SCHEDULED_INTERVAL_STR

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN is not set")
        raise ValueError("TELEGRAM_TOKEN environment variable is required")

    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).job_queue(JobQueue()).build()
        logger.info("Application initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize application: {e}")
        raise

    # Initialize data (settings and groups)
    # Load existing settings, or provide a default structure
    settings = load_json(SETTINGS_FILE, {
        "highest_milestone_achieved": 0, # New setting for milestone tracking
    })
    # Ensure hardcoded URLs are always set, even if settings.json exists
    settings["default_image_url"] = DEFAULT_IMAGE_URL
    settings["scheduled_and_check_price_image_url"] = SCHEDULED_AND_CHECK_PRICE_IMAGE_URL
    save_json(SETTINGS_FILE, settings) # Save immediately to persist new default structure if it's new

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
    app.add_handler(CommandHandler("lanlan", lanlan_command)) # Simple command
    app.add_handler(CommandHandler("setimage", setimage)) # Deprecated command
    app.add_handler(CommandHandler("setschedule", setschedule)) # New admin command
    app.add_handler(CommandHandler("whomadethebot", whomadethebot))
    app.add_handler(CommandHandler("help", help_command)) # Help works directly now

    # Callback query handlers for inline buttons
    # Note: 'start_lanlan_calculation' now just gives instructions for the /lanlan command
    app.add_handler(CallbackQueryHandler(button_handler, pattern='^(check_lanlan_price|start_lanlan_calculation)$'))
    app.add_handler(CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main$'))

    # Schedule recurring job
    try:
        job_queue: JobQueue = app.job_queue
        # Check if SCHEDULED_INTERVAL is valid before scheduling
        if SCHEDULED_INTERVAL is not None and SCHEDULED_INTERVAL > 0:
            job_queue.run_repeating(scheduled_job, interval=SCHEDULED_INTERVAL, first=SCHEDULED_FIRST, name="scheduled_price_update")
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
