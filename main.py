import os
import asyncio
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from collections import defaultdict

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from playwright.async_api import async_playwright
from flask import Flask

# =====================================================
# 🌐 HEALTH CHECK SERVER
# =====================================================
app = Flask(__name__)
bot_status = {"running": False, "last_update": None, "records": 0}

@app.route('/')
@app.route('/health')
def health():
    return {
        "status": "healthy",
        "service": "wingo-bot",
        "bot_running": bot_status["running"],
        "last_update": str(bot_status.get("last_update", "N/A")),
        "records": bot_status.get("records", 0)
    }, 200

@app.route('/stats')
def stats():
    try:
        count = col.count_documents({})
        bot_status["records"] = count
        return {"records": count, "status": "running"}, 200
    except Exception as e:
        return {"error": str(e)}, 503

def run_flask():
    port = int(os.getenv("PORT", 10000))
    print(f"[FLASK] Starting on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# =====================================================
# 🔐 ENV LOADING
# =====================================================
print("[BOOT] Starting Wingo Bot...")

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
    print("[ENV] .env loaded")
else:
    print("[ENV] No .env file, using system environment")

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
MONGO_URI = os.getenv("MONGO_URI")

print("ENV CHECK:",
      "TG_TOKEN", bool(TG_TOKEN),
      "ADMIN_CHAT_ID", bool(ADMIN_CHAT_ID),
      "MONGO_URI", bool(MONGO_URI))

if not all([TG_TOKEN, ADMIN_CHAT_ID, MONGO_URI]):
    raise Exception("❌ Missing env variables")

# =====================================================
# ⚙️ CONFIG (OPTIMIZED FOR FREE TIER)
# =====================================================
WINGO_URL = "https://wingoanalyst.com/#/wingo_1m"
CHECK_INTERVAL = 5
MAX_RECORDS = 500  # Increased for better analysis
MIN_DATA_FOR_CALC = 100
MIN_MARKOV_DATA = 200  # Need more for Markov
CACHE_DURATION = 300  # Cache calculations for 5 min

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# In-memory cache to reduce DB queries
analysis_cache = {}
last_cache_time = 0

# =====================================================
# 📤 TELEGRAM (MULTI-USER)
# =====================================================
def tg_send(text, chat_id=None):
    """Send message to specific chat or all active users"""
    if chat_id:
        _send_to_chat(chat_id, text)
    else:
        # Broadcast to all active users (optimized query)
        active_users = users_col.find({"active": True}, {"chat_id": 1})
        for user in active_users:
            _send_to_chat(user["chat_id"], text)

def _send_to_chat(chat_id, text):
    """Internal function to send to a single chat"""
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
        print(f"[TG] ✓ {chat_id}")
    except Exception as e:
        print(f"[TG ERROR] {chat_id}: {e}")

def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID)

# =====================================================
# 🗄️ MONGODB (OPTIMIZED)
# =====================================================
print("[DB] Connecting to MongoDB...")
try:
    mongo = MongoClient(
        MONGO_URI, 
        serverSelectionTimeoutMS=5000,
        maxPoolSize=10,  # Limit connections for free tier
        retryWrites=True
    )
    mongo.admin.command('ping')
    print("[DB] Connected successfully")
except Exception as e:
    print(f"[DB ERROR] Failed to connect: {e}")
    raise

db = mongo["wingo_bot"]
col = db["results"]
settings_col = db["settings"]
users_col = db["users"]
predictions_col = db["predictions"]

# Indexes
col.create_index([("period", ASCENDING)], unique=True)
col.create_index([("timestamp", -1)])  # For sorting
users_col.create_index([("chat_id", ASCENDING)], unique=True)
predictions_col.create_index([("timestamp", -1)])
print("[DB] Indexes ensured")

# Initialize admin
if not users_col.find_one({"chat_id": ADMIN_CHAT_ID}):
    users_col.insert_one({
        "chat_id": ADMIN_CHAT_ID,
        "username": "admin",
        "active": True,
        "is_admin": True,
        "added_at": datetime.now(timezone.utc)
    })
    print(f"[DB] Admin user added: {ADMIN_CHAT_ID}")

# Default settings
if not settings_col.find_one({"_id": "global"}):
    settings_col.insert_one({
        "_id": "global",
        "alerts": True,
        "probability": True,
        "advanced_mode": True
    })
    print("[DB] Default settings inserted")

def get_settings():
    return settings_col.find_one({"_id": "global"})

# =====================================================
# 🔄 DB HELPERS
# =====================================================
def trim_db():
    """Trim old records (keeps last 500)"""
    count = col.count_documents({})
    if count > MAX_RECORDS:
        extra = count - MAX_RECORDS
        old_ids = [x["_id"] for x in col.find({}, {"_id": 1}).sort("timestamp", 1).limit(extra)]
        if old_ids:
            col.delete_many({"_id": {"$in": old_ids}})
            print(f"[DB] Trimmed {len(old_ids)} old records")
    
    # Trim old predictions (keep last 200)
    pred_count = predictions_col.count_documents({})
    if pred_count > 200:
        extra = pred_count - 200
        old_pred_ids = [x["_id"] for x in predictions_col.find({}, {"_id": 1}).sort("timestamp", 1).limit(extra)]
        if old_pred_ids:
            predictions_col.delete_many({"_id": {"$in": old_pred_ids}})

# =====================================================
# 🧮 OPTIMIZED ADVANCED CALCULATIONS
# =====================================================
def get_cached_data():
    """Get data with caching to reduce DB load"""
    global analysis_cache, last_cache_time
    
    current_time = time.time()
    if current_time - last_cache_time < CACHE_DURATION and analysis_cache:
        return analysis_cache
    
    # Fetch only needed fields
    data = list(col.find({}, {"result": 1, "timestamp": 1}).sort("timestamp", 1))
    analysis_cache = data
    last_cache_time = current_time
    return data

def build_markov_chain(results):
    """Build Markov transition matrix"""
    if len(results) < 50:
        return None
    
    transitions = {"Big→Big": 0, "Big→Small": 0, "Small→Small": 0, "Small→Big": 0}
    
    for i in range(len(results) - 1):
        key = f"{results[i]}→{results[i+1]}"
        transitions[key] += 1
    
    big_total = transitions["Big→Big"] + transitions["Big→Small"]
    small_total = transitions["Small→Small"] + transitions["Small→Big"]
    
    return {
        "Big": {
            "Big": transitions["Big→Big"] / big_total if big_total > 0 else 0.5,
            "Small": transitions["Big→Small"] / big_total if big_total > 0 else 0.5
        },
        "Small": {
            "Small": transitions["Small→Small"] / small_total if small_total > 0 else 0.5,
            "Big": transitions["Small→Big"] / small_total if small_total > 0 else 0.5
        }
    }

def analyze_time_patterns(data):
    """Detect time-based biases (optimized)"""
    if len(data) < 200:
        return None
    
    hourly = defaultdict(lambda: {"Big": 0, "Small": 0})
    
    for record in data:
        hour = record["timestamp"].hour
        hourly[hour][record["result"]] += 1
    
    current_hour = datetime.now().hour
    if current_hour in hourly:
        counts = hourly[current_hour]
        total = counts["Big"] + counts["Small"]
        if total >= 10:
            big_pct = counts["Big"] / total * 100
            if abs(big_pct - 50) > 8:  # >8% deviation
                return {
                    "bias": "Big" if big_pct > 50 else "Small",
                    "strength": abs(big_pct - 50)
                }
    return None

def calculate_streak_decay(streak_len):
    """Exponential decay for long streaks"""
    if streak_len <= 3:
        return 1.0
    return 0.87 ** (streak_len - 3)

def advanced_prediction(target, streak_len):
    """Enhanced ensemble prediction model"""
    data = get_cached_data()
    results = [x["result"] for x in data]
    total = len(results)
    
    if total < MIN_DATA_FOR_CALC:
        return None
    
    # 1. Base historical analysis
    matched = 0
    continued = 0
    
    for i in range(len(results) - streak_len):
        window = results[i:i + streak_len]
        if all(x == target for x in window):
            matched += 1
            if i + streak_len < len(results) and results[i + streak_len] == target:
                continued += 1
    
    if matched == 0:
        return None
    
    base_continue = (continued / matched) * 100
    
    # 2. Markov chain (if enough data)
    markov_continue = None
    if total >= MIN_MARKOV_DATA:
        markov = build_markov_chain(results)
        if markov:
            markov_continue = markov[target][target] * 100
    
    # 3. Streak decay factor
    decay = calculate_streak_decay(streak_len)
    
    # 4. Recent trend (last 30%)
    recent_results = results[int(total * 0.7):]
    recent_target_pct = (recent_results.count(target) / len(recent_results)) * 100
    recent_multiplier = 1.0 + ((recent_target_pct - 50) / 100)
    
    # 5. Time pattern
    time_pattern = analyze_time_patterns(data)
    time_multiplier = 1.0
    if time_pattern and time_pattern["bias"] == target:
        time_multiplier = 1.0 + (time_pattern["strength"] / 200)
    elif time_pattern:
        time_multiplier = 1.0 - (time_pattern["strength"] / 200)
    
    # Ensemble calculation
    if markov_continue is not None:
        # Use all models
        adjusted_continue = (
            base_continue * 0.35 +        # Historical
            markov_continue * 0.30 +      # Markov
            (base_continue * decay) * 0.35 # Decay
        ) * recent_multiplier * time_multiplier
    else:
        # Use limited models
        adjusted_continue = (
            base_continue * 0.50 +
            (base_continue * decay) * 0.50
        ) * recent_multiplier * time_multiplier
    
    # Cap probabilities
    adjusted_continue = min(92, max(8, adjusted_continue))
    adjusted_break = 100 - adjusted_continue
    
    # Calculate confidence
    sample_strength = min(matched / 40, 1) * 25
    bias_strength = abs(adjusted_continue - 50) * 0.6
    decay_impact = (1 - decay) * 20
    recent_impact = abs(recent_target_pct - 50) * 0.3
    
    confidence_score = sample_strength + bias_strength + decay_impact + recent_impact
    
    if confidence_score >= 70:
        confidence = "🔥 Very High"
    elif confidence_score >= 55:
        confidence = "💪 High"
    elif confidence_score >= 40:
        confidence = "⚖️ Moderate"
    else:
        confidence = "⚠️ Low"
    
    # Calculate pressure
    streaks = []
    cur, cnt = results[0], 1
    for x in results[1:]:
        if x == cur:
            cnt += 1
        else:
            streaks.append(cnt)
            cur, cnt = x, 1
    streaks.append(cnt)
    avg_streak = sum(streaks) / len(streaks)
    pressure = streak_len / avg_streak if avg_streak else 1
    
    return {
        "continue": round(adjusted_continue, 2),
        "break": round(adjusted_break, 2),
        "confidence": confidence,
        "score": round(confidence_score, 1),
        "pressure": round(pressure, 2),
        "matched": matched,
        "continued": continued,
        "models_used": 4 if markov_continue else 3,
        "time_bias": time_pattern["bias"] if time_pattern else None
    }

def get_prediction_accuracy():
    """Calculate recent prediction accuracy"""
    recent = list(predictions_col.find().sort("timestamp", -1).limit(100))
    
    if len(recent) < 20:
        return None
    
    correct = sum(1 for p in recent if p.get("correct", False))
    accuracy = (correct / len(recent)) * 100
    
    return {
        "accuracy": round(accuracy, 1),
        "sample": len(recent),
        "beating_random": accuracy > 52
    }

# =====================================================
# 📥 SCRAPER HELPERS
# =====================================================
async def bootstrap_history(page):
    existing_count = col.count_documents({})
    
    if existing_count >= 50:
        print(f"[BOOTSTRAP] Skipping - DB has {existing_count} records")
        return
    
    print("[BOOTSTRAP] Loading history...")
    rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
    
    inserted = 0
    for r in rows:
        try:
            text = await r.inner_text()
            parts = [p.strip() for p in text.split("\n") if p.strip()]
            if len(parts) < 3:
                continue
            
            period = parts[0].replace("*", "")
            result = parts[2]
            
            if result not in ("Big", "Small"):
                continue
            
            if col.find_one({"period": period}):
                continue
            
            try:
                col.insert_one({
                    "period": period,
                    "result": result,
                    "timestamp": datetime.now(timezone.utc)
                })
                inserted += 1
            except DuplicateKeyError:
                continue
        except:
            continue
    
    print(f"[BOOTSTRAP] Inserted {inserted} records")

async def extract_latest(page):
    rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
    
    for r in rows:
        try:
            text = await r.inner_text()
            parts = [p.strip() for p in text.split("\n") if p.strip()]
            if len(parts) >= 3:
                period = parts[0].replace("*", "")
                result = parts[2]
                if result in ("Big", "Small"):
                    return period, result
        except:
            continue
    return None

# =====================================================
# 🤖 TELEGRAM COMMANDS
# =====================================================
def command_listener():
    print("[TG] Command listener started")
    offset = 0
    
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset + 1, "timeout": 30},
                timeout=35
            ).json()
            
            for u in r.get("result", []):
                offset = u["update_id"]
                msg = u.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                username = msg.get("chat", {}).get("username", "unknown")
                
                if not text or not chat_id:
                    continue
                
                print(f"[TG CMD] {chat_id}: {text}")
                
                # Auto-register
                if not users_col.find_one({"chat_id": str(chat_id)}):
                    users_col.insert_one({
                        "chat_id": str(chat_id),
                        "username": username,
                        "active": False,
                        "is_admin": False,
                        "added_at": datetime.now(timezone.utc)
                    })
                
                # ADMIN COMMANDS
                if text.startswith("/adduser") and is_admin(chat_id):
                    parts = text.split()
                    if len(parts) != 2:
                        tg_send("❌ *Usage:* `/adduser {chat_id}`", chat_id)
                        continue
                    
                    target_id = parts[1]
                    users_col.update_one(
                        {"chat_id": target_id},
                        {"$set": {"active": True, "added_at": datetime.now(timezone.utc)}},
                        upsert=True
                    )
                    tg_send(f"✅ *User Added*\n👤 `{target_id}`", chat_id)
                    tg_send("🎉 *Welcome!*\n✨ You've been activated by admin\n\nType /help", target_id)
                
                elif text.startswith("/removeuser") and is_admin(chat_id):
                    parts = text.split()
                    if len(parts) == 2:
                        target_id = parts[1]
                        users_col.update_one({"chat_id": target_id}, {"$set": {"active": False}})
                        tg_send(f"🚫 *User Removed*\n👤 `{target_id}`", chat_id)
                
                elif text == "/listusers" and is_admin(chat_id):
                    users = list(users_col.find({"active": True}, {"chat_id": 1, "username": 1, "is_admin": 1}))
                    if not users:
                        tg_send("📭 *No active users*", chat_id)
                    else:
                        msg = "👥 *ACTIVE USERS*\n\n"
                        for i, u in enumerate(users, 1):
                            badge = " 👑" if u.get("is_admin") else ""
                            msg += f"{i}. `{u['chat_id']}`{badge}\n"
                        msg += f"\n📊 Total: *{len(users)}*"
                        tg_send(msg, chat_id)
                
                elif text == "/accuracy" and is_admin(chat_id):
                    acc = get_prediction_accuracy()
                    if acc:
                        status = "🎯 Beating Random!" if acc["beating_random"] else "📊 Learning..."
                        tg_send(
                            f"📈 *PREDICTION ACCURACY*\n\n"
                            f"{status}\n"
                            f"Accuracy: *{acc['accuracy']}%*\n"
                            f"Sample: *{acc['sample']} predictions*",
                            chat_id
                        )
                    else:
                        tg_send("⏳ Need 20+ predictions first", chat_id)
                
                # USER COMMANDS
                elif text == "/start":
                    user = users_col.find_one({"chat_id": str(chat_id)})
                    if user and user.get("active"):
                        tg_send("🎰 *WINGO BOT* 🎰\n\n✅ Active\n\n/help - Commands", chat_id)
                    else:
                        tg_send(f"🎰 *WINGO BOT*\n\n⚠️ Not authorized\n\nYour ID: `{chat_id}`", chat_id)
                
                elif text == "/help":
                    msg = "📚 *COMMANDS*\n\n/stats\n/settings\n/mychatid"
                    if is_admin(chat_id):
                        msg += "\n\n👑 *ADMIN*\n/adduser {id}\n/removeuser {id}\n/listusers\n/accuracy"
                    tg_send(msg, chat_id)
                
                elif text == "/stats":
                    count = col.count_documents({})
                    users_count = users_col.count_documents({"active": True})
                    acc = get_prediction_accuracy()
                    
                    msg = f"📊 *STATS*\n\n🎲 Records: *{count}*\n👥 Users: *{users_count}*"
                    if acc:
                        msg += f"\n🎯 Accuracy: *{acc['accuracy']}%*"
                    tg_send(msg, chat_id)
                
                elif text == "/settings":
                    s = get_settings()
                    tg_send(
                        f"⚙️ *SETTINGS*\n\n"
                        f"🔔 Alerts: *{'ON' if s['alerts'] else 'OFF'}*\n"
                        f"📈 Probability: *{'ON' if s['probability'] else 'OFF'}*\n"
                        f"🧠 Advanced: *{'ON' if s.get('advanced_mode', True) else 'OFF'}*",
                        chat_id
                    )
                
                elif text == "/mychatid":
                    tg_send(f"🆔 *YOUR ID*\n\n`{chat_id}`\n\n📋 Tap to copy", chat_id)
        
        except Exception as e:
            print(f"[TG CMD ERROR] {e}")
        
        time.sleep(2)

# =====================================================
# 🚀 MAIN MONITOR
# =====================================================
async def monitor(page):
    print("[MONITOR] Starting...")
    bot_status["running"] = True
    
    current_side = None
    current_streak = 0
    last_alerted_streak = 0
    last_prediction = None
    
    while True:
        try:
            res = await extract_latest(page)
            if not res:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            
            period, side = res
            
            if col.find_one({"period": period}):
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            
            # Track prediction accuracy
            if last_prediction:
                was_correct = last_prediction["side"] == side
                predictions_col.insert_one({
                    "period": period,
                    "predicted": last_prediction["side"],
                    "actual": side,
                    "correct": was_correct,
                    "confidence": last_prediction.get("confidence", "Unknown"),
                    "timestamp": datetime.now(timezone.utc)
                })
                last_prediction = None
            
            try:
                col.insert_one({
                    "period": period,
                    "result": side,
                    "timestamp": datetime.now(timezone.utc)
                })
                bot_status["last_update"] = datetime.now(timezone.utc)
                print(f"[DATA] {period} | {side}")
            except DuplicateKeyError:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            
            trim_db()
            
            # Invalidate cache
            global last_cache_time
            last_cache_time = 0
            
            if side == current_side:
                current_streak += 1
            else:
                if current_streak >= 3 and current_side is not None:
                    prev_emoji = "🔴" if current_side == "Big" else "🔵"
                    new_emoji = "🔵" if side == "Small" else "🔴"
                    tg_send(
                        f"💔 *STREAK BROKEN* 💔\n\n"
                        f"{prev_emoji} Was: *{current_streak}x {current_side.upper()}*\n"
                        f"{new_emoji} Now: *{side.upper()}*\n\n"
                        f"⏱️ {datetime.now().strftime('%H:%M:%S')}"
                    )
                
                current_side = side
                current_streak = 1
                last_alerted_streak = 0
            
            settings = get_settings()
            total = col.count_documents({})
            
            if (current_streak >= 3 and 
                current_streak > last_alerted_streak and 
                settings["alerts"]):
                
                emoji = "🔴" if current_side == "Big" else "🔵"
                fire = "🔥" * min(current_streak, 5)
                
                msg = (
                    f"{fire} *STREAK ALERT* {fire}\n\n"
                    f"{emoji} *{current_streak}x {current_side.upper()}* {emoji}\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📊 Records: *{total}*\n"
                    f"⏱️ {datetime.now().strftime('%H:%M:%S')}"
                )
                
                if settings["probability"] and total >= MIN_DATA_FOR_CALC:
                    pred = advanced_prediction(current_side, current_streak)
                    if pred:
                        cont_bars = "█" * int(pred["continue"] / 10) + "░" * (10 - int(pred["continue"] / 10))
                        brk_bars = "█" * int(pred["break"] / 10) + "░" * (10 - int(pred["break"] / 10))
                        
                        msg += (
                            f"\n\n📈 *ADVANCED ANALYSIS*\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"✅ Continue: *{pred['continue']}%*\n"
                            f"   {cont_bars}\n\n"
                            f"❌ Break: *{pred['break']}%*\n"
                            f"   {brk_bars}\n\n"
                            f"⚡ Pressure: *{pred['pressure']}×*\n"
                            f"🎯 Confidence: *{pred['confidence']}*\n"
                            f"🧠 Models: *{pred['models_used']}*\n"
                            f"💯 Score: *{pred['score']}/100*"
                        )
                        
                        if pred["time_bias"]:
                            msg += f"\n⏰ Hour Bias: *{pred['time_bias']}*"
                        
                        # Store prediction
                        predicted_side = current_side if pred["continue"] > pred["break"] else ("Small" if current_side == "Big" else "Big")
                        last_prediction = {
                            "side": predicted_side,
                            "confidence": pred["confidence"]
                        }
                
                elif total < MIN_DATA_FOR_CALC:
                    msg += (
                        f"\n\n⏳ *COLLECTING DATA*\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"📊 Progress: *{total}/{MIN_DATA_FOR_CALC}*\n"
                        f"🔜 Advanced analysis soon!"
                    )
                
                tg_send(msg)
                last_alerted_streak = current_streak
            
            await asyncio.sleep(CHECK_INTERVAL)
        
        except Exception as e:
            print(f"[MONITOR ERROR] {e}")
            bot_status["running"] = False
            await asyncio.sleep(10)
            bot_status["running"] = True

# =====================================================
# ▶ RUN
# =====================================================
async def main():
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    await asyncio.sleep(2)
    
    try:
        tg_send(
            "🚀 *WINGO BOT v2.0* 🚀\n\n"
            "✨ Advanced prediction online\n"
            "🧠 Multi-model ensemble\n"
            "📊 Accuracy tracking enabled\n\n"
            f"⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    except Exception as e:
        print(f"[TG] Startup message failed: {e}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        page = await browser.new_page()
        
        print("[SCRAPER] Loading page...")
        await page.goto(WINGO_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        
        await bootstrap_history(page)
        
        await asyncio.gather(
            monitor(page),
            asyncio.to_thread(command_listener)
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Bot stopped")
        bot_status["running"] = False
    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        bot_status["running"] = False
        raise
