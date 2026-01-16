import os
import asyncio
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from collections import defaultdict, Counter

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from playwright.async_api import async_playwright
from flask import Flask

# =====================================================
# 🌐 HEALTH CHECK SERVER
# =====================================================
app = Flask(__name__)
bot_status = {"running": False, "last_update": None, "records": 0, "started": False}

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
    print("[ENV] Using system environment")

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
MONGO_URI = os.getenv("MONGO_URI")

if not all([TG_TOKEN, ADMIN_CHAT_ID, MONGO_URI]):
    raise Exception("❌ Missing env variables")

# =====================================================
# ⚙️ CONFIG
# =====================================================
WINGO_URL = "https://wingoanalyst.com/#/wingo_1m"
CHECK_INTERVAL = 5
MAX_RECORDS = 1000  # Increased for better analysis
MAX_PREDICTIONS = 500  # Store last 500 predictions
MIN_DATA_FOR_CALC = 100  # Lowered to start predicting sooner
MIN_CONFIDENCE_TO_ALERT = 55  # Lowered to 55 so it actually sends predictions
CACHE_DURATION = 300

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# Cache
analysis_cache = {}
last_cache_time = 0

# =====================================================
# 📤 TELEGRAM
# =====================================================
def tg_send(text, chat_id=None):
    if chat_id:
        _send_to_chat(chat_id, text)
    else:
        active_users = users_col.find({"active": True}, {"chat_id": 1})
        for user in active_users:
            _send_to_chat(user["chat_id"], text)

def _send_to_chat(chat_id, text):
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        print(f"[TG] ✓ {chat_id}")
    except Exception as e:
        print(f"[TG ERROR] {chat_id}: {e}")

def is_admin(chat_id):
    return str(chat_id) == str(ADMIN_CHAT_ID)

# =====================================================
# 🗄️ MONGODB
# =====================================================
print("[DB] Connecting to MongoDB...")
try:
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, maxPoolSize=10)
    mongo.admin.command('ping')
    print("[DB] Connected")
except Exception as e:
    print(f"[DB ERROR] {e}")
    raise

db = mongo["wingo_bot"]
col = db["results"]
settings_col = db["settings"]
users_col = db["users"]
predictions_col = db["predictions"]

col.create_index([("period", ASCENDING)], unique=True)
col.create_index([("timestamp", -1)])
users_col.create_index([("chat_id", ASCENDING)], unique=True)
predictions_col.create_index([("timestamp", -1)])
print("[DB] Indexes OK")

if not users_col.find_one({"chat_id": ADMIN_CHAT_ID}):
    users_col.insert_one({
        "chat_id": ADMIN_CHAT_ID,
        "username": "admin",
        "active": True,
        "is_admin": True,
        "added_at": datetime.now(timezone.utc)
    })

if not settings_col.find_one({"_id": "global"}):
    settings_col.insert_one({
        "_id": "global",
        "alerts": True,
        "probability": True,
        "bet_alerts": True  # New: send bet recommendations
    })

def get_settings():
    return settings_col.find_one({"_id": "global"})

# =====================================================
# 🔄 DB HELPERS
# =====================================================
def trim_db():
    count = col.count_documents({})
    if count > MAX_RECORDS:
        extra = count - MAX_RECORDS
        old_ids = [x["_id"] for x in col.find({}, {"_id": 1}).sort("timestamp", 1).limit(extra)]
        if old_ids:
            col.delete_many({"_id": {"$in": old_ids}})
    
    pred_count = predictions_col.count_documents({})
    if pred_count > MAX_PREDICTIONS:
        extra = pred_count - MAX_PREDICTIONS
        old_pred = [x["_id"] for x in predictions_col.find({}, {"_id": 1}).sort("timestamp", 1).limit(extra)]
        if old_pred:
            predictions_col.delete_many({"_id": {"$in": old_pred}})

# =====================================================
# 🧮 IMPROVED PREDICTION ENGINE
# =====================================================
def get_cached_data():
    global analysis_cache, last_cache_time
    current_time = time.time()
    
    if current_time - last_cache_time < CACHE_DURATION and analysis_cache:
        return analysis_cache
    
    data = list(col.find({}, {"result": 1, "timestamp": 1}).sort("timestamp", 1))
    analysis_cache = data
    last_cache_time = current_time
    return data

def detect_manipulation(results):
    """Enhanced manipulation detection"""
    if len(results) < 150:
        return {"rigged": False, "confidence": 0}
    
    # Pattern 1: Excessive alternation (rigged = breaks patterns often)
    alternations = sum(1 for i in range(1, len(results)) if results[i] != results[i-1])
    alt_rate = alternations / len(results)
    
    # Pattern 2: Trap points (3x, 5x, 7x break more than neighbors)
    streak_breaks = defaultdict(int)
    current_streak = 1
    
    for i in range(1, len(results)):
        if results[i] == results[i-1]:
            current_streak += 1
        else:
            if current_streak <= 10:
                streak_breaks[current_streak] += 1
            current_streak = 1
    
    trap_signal = 0
    if streak_breaks[3] > streak_breaks[2] * 1.2 and streak_breaks[3] > streak_breaks[4] * 1.2:
        trap_signal += 1
    if streak_breaks[5] > streak_breaks[4] * 1.15 and streak_breaks[5] > streak_breaks[6] * 1.15:
        trap_signal += 1
    
    # Pattern 3: Check if distribution is suspiciously balanced
    big_count = results.count("Big")
    balance = abs(big_count / len(results) - 0.5)
    forced_balance = balance < 0.08  # Too perfect = rigged
    
    manipulation_score = 0
    if alt_rate > 0.58:
        manipulation_score += 35
    if trap_signal >= 2:
        manipulation_score += 30
    if forced_balance:
        manipulation_score += 20
    if alt_rate > 0.62:
        manipulation_score += 15
    
    is_rigged = manipulation_score >= 50
    
    return {
        "rigged": is_rigged,
        "score": manipulation_score,
        "alt_rate": round(alt_rate * 100, 1),
        "trap_detected": trap_signal >= 1,
        "forced_balance": forced_balance
    }

def pattern_frequency_analysis(results, pattern_length=3):
    """Find most common patterns and their outcomes"""
    if len(results) < 200:
        return {}
    
    pattern_outcomes = defaultdict(lambda: {"Big": 0, "Small": 0})
    
    for i in range(len(results) - pattern_length):
        pattern = tuple(results[i:i+pattern_length])
        if i + pattern_length < len(results):
            next_result = results[i + pattern_length]
            pattern_outcomes[pattern][next_result] += 1
    
    # Convert to probabilities
    pattern_probs = {}
    for pattern, outcomes in pattern_outcomes.items():
        total = outcomes["Big"] + outcomes["Small"]
        if total >= 5:  # Minimum sample
            pattern_probs[pattern] = {
                "Big": round(outcomes["Big"] / total * 100, 1),
                "Small": round(outcomes["Small"] / total * 100, 1),
                "sample": total
            }
    
    return pattern_probs

def advanced_prediction_v2(target, streak_len, results):
    """Significantly improved prediction algorithm - MORE AGGRESSIVE"""
    total = len(results)
    
    if total < MIN_DATA_FOR_CALC:
        return None
    
    # 1. Historical streak analysis
    matched = continued = 0
    for i in range(len(results) - streak_len):
        if all(x == target for x in results[i:i+streak_len]):
            matched += 1
            if i + streak_len < len(results) and results[i+streak_len] == target:
                continued += 1
    
    if matched == 0:
        return None
    
    base_continue = (continued / matched) * 100
    
    # 2. Pattern frequency (last 3 results pattern)
    pattern_probs = pattern_frequency_analysis(results)
    recent_pattern = tuple(results[-min(3, len(results)):])
    pattern_boost = 0
    
    if recent_pattern in pattern_probs:
        pattern_data = pattern_probs[recent_pattern]
        if pattern_data["sample"] >= 5:  # Lowered threshold
            expected_continue = pattern_data[target]
            pattern_boost = (expected_continue - 50) * 0.6  # Increased weight
    
    # 3. Manipulation detection
    manipulation = detect_manipulation(results)
    
    # 4. Recent momentum (last 25%)
    recent_results = results[int(total * 0.75):]
    recent_target_count = recent_results.count(target)
    recent_momentum = (recent_target_count / len(recent_results)) * 100
    momentum_boost = (recent_momentum - 50) * 0.5  # Direct boost
    
    # 5. Streak decay (adjusted to be less aggressive)
    if streak_len <= 2:
        decay = 1.0
    elif streak_len == 3:
        decay = 0.95  # Less penalty
    elif streak_len == 4:
        decay = 0.90
    elif streak_len == 5:
        decay = 0.82
    elif streak_len == 6:
        decay = 0.75
    else:
        decay = 0.72 ** (streak_len - 6)
    
    # 6. Anti-manipulation logic (less aggressive)
    if manipulation["rigged"]:
        trap_points = {3: -5, 5: -8, 7: -12}  # Reduced penalties
        trap_penalty = trap_points.get(streak_len, 0)
        
        if streak_len >= 4:
            manipulation_penalty = -5 * (streak_len - 3)  # Reduced
        else:
            manipulation_penalty = 0
    else:
        trap_penalty = 0
        manipulation_penalty = 0
    
    # 7. Combine all factors (more aggressive)
    adjusted_continue = (
        base_continue * 0.5 +           # Base weight
        base_continue * decay * 0.3 +   # Decay component
        pattern_boost +                 # Pattern signal
        momentum_boost +                # Momentum signal
        manipulation_penalty +          # Rigging adjustment
        trap_penalty
    )
    
    # Cap between 15-85% (wider range)
    adjusted_continue = min(85, max(15, adjusted_continue))
    adjusted_break = 100 - adjusted_continue
    
    # 8. Calculate confidence (MORE GENEROUS)
    sample_quality = min(matched / 30, 1) * 25  # Easier to get points
    deviation_strength = abs(adjusted_continue - 50) * 0.9  # More weight to deviation
    pattern_confidence = abs(pattern_boost) * 0.8 if pattern_boost != 0 else 0
    momentum_confidence = abs(momentum_boost) * 0.6
    manipulation_clarity = manipulation["score"] * 0.2 if manipulation["rigged"] else 10  # Bonus if rigged
    
    confidence_score = (
        sample_quality + 
        deviation_strength + 
        pattern_confidence + 
        momentum_confidence + 
        manipulation_clarity
    )
    
    # More lenient confidence levels
    if confidence_score >= 70:
        confidence = "🔥 Very High"
    elif confidence_score >= 55:
        confidence = "💪 High"
    elif confidence_score >= 40:
        confidence = "⚖️ Moderate"
    else:
        confidence = "⚠️ Low"
    
    # Determine recommendation
    if adjusted_continue > adjusted_break:
        recommendation = target
        rec_probability = adjusted_continue
    else:
        recommendation = "Small" if target == "Big" else "Big"
        rec_probability = adjusted_break
    
    return {
        "continue": round(adjusted_continue, 1),
        "break": round(adjusted_break, 1),
        "confidence": confidence,
        "score": round(confidence_score, 1),
        "matched": matched,
        "continued": continued,
        "recommendation": recommendation,
        "rec_probability": round(rec_probability, 1),
        "manipulation_detected": manipulation["rigged"],
        "should_alert": confidence_score >= MIN_CONFIDENCE_TO_ALERT,  # More alerts
        "pattern_boost": round(pattern_boost, 1),
        "momentum_boost": round(momentum_boost, 1)
    }


def get_prediction_accuracy(limit=100):
    """Get accuracy for last N predictions"""
    recent = list(predictions_col.find().sort("timestamp", -1).limit(limit))
    
    if len(recent) < 10:
        return None
    
    correct = sum(1 for p in recent if p.get("correct", False))
    accuracy = (correct / len(recent)) * 100
    
    # Breakdown by confidence level
    by_confidence = defaultdict(lambda: {"correct": 0, "total": 0})
    for p in recent:
        conf = p.get("confidence_level", "Unknown")
        by_confidence[conf]["total"] += 1
        if p.get("correct"):
            by_confidence[conf]["correct"] += 1
    
    confidence_breakdown = {}
    for conf, stats in by_confidence.items():
        if stats["total"] >= 3:
            confidence_breakdown[conf] = round(stats["correct"] / stats["total"] * 100, 1)
    
    return {
        "accuracy": round(accuracy, 1),
        "sample": len(recent),
        "correct": correct,
        "wrong": len(recent) - correct,
        "by_confidence": confidence_breakdown
    }

# =====================================================
# 📥 SCRAPER
# =====================================================
async def bootstrap_history(page):
    existing = col.count_documents({})
    if existing >= 50:
        print(f"[BOOTSTRAP] Skip - have {existing}")
        return
    
    print("[BOOTSTRAP] Loading...")
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
            
            if result in ("Big", "Small") and not col.find_one({"period": period}):
                try:
                    col.insert_one({
                        "period": period,
                        "result": result,
                        "timestamp": datetime.now(timezone.utc)
                    })
                    inserted += 1
                except DuplicateKeyError:
                    pass
        except:
            pass
    
    print(f"[BOOTSTRAP] +{inserted}")

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
            pass
    return None

# =====================================================
# 🤖 TELEGRAM COMMANDS
# =====================================================
def command_listener():
    print("[TG] Commands listening")
    offset = 0
    
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset+1, "timeout": 30}, timeout=35).json()
            
            for u in r.get("result", []):
                offset = u["update_id"]
                msg = u.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id")
                username = msg.get("chat", {}).get("username", "unknown")
                
                if not text or not chat_id:
                    continue
                
                print(f"[CMD] {chat_id}: {text}")
                
                if not users_col.find_one({"chat_id": str(chat_id)}):
                    users_col.insert_one({
                        "chat_id": str(chat_id),
                        "username": username,
                        "active": False,
                        "is_admin": False,
                        "added_at": datetime.now(timezone.utc)
                    })
                
                # ADMIN
                if text.startswith("/adduser") and is_admin(chat_id):
                    parts = text.split()
                    if len(parts) == 2:
                        target = parts[1]
                        users_col.update_one({"chat_id": target}, {"$set": {"active": True}}, upsert=True)
                        tg_send(f"✅ Added: `{target}`", chat_id)
                        tg_send("🎉 *Welcome!* You're activated\n/help", target)
                
                elif text.startswith("/removeuser") and is_admin(chat_id):
                    parts = text.split()
                    if len(parts) == 2:
                        users_col.update_one({"chat_id": parts[1]}, {"$set": {"active": False}})
                        tg_send(f"🚫 Removed: `{parts[1]}`", chat_id)
                
                elif text == "/listusers" and is_admin(chat_id):
                    users = list(users_col.find({"active": True}, {"chat_id": 1, "is_admin": 1}))
                    if users:
                        msg = "👥 *USERS*\n\n"
                        for i, u in enumerate(users, 1):
                            badge = " 👑" if u.get("is_admin") else ""
                            msg += f"{i}. `{u['chat_id']}`{badge}\n"
                        msg += f"\n📊 Total: {len(users)}"
                        tg_send(msg, chat_id)
                    else:
                        tg_send("📭 No users", chat_id)
                
                elif text == "/accuracy" and is_admin(chat_id):
                    acc_100 = get_prediction_accuracy(100)
                    acc_500 = get_prediction_accuracy(500)
                    
                    msg = "📈 *ACCURACY REPORT*\n\n"
                    
                    if acc_100:
                        msg += f"📊 *Last 100:*\n"
                        msg += f"✓ {acc_100['correct']} | ✗ {acc_100['wrong']}\n"
                        msg += f"Accuracy: *{acc_100['accuracy']}%*\n\n"
                    
                    if acc_500 and acc_500['sample'] > 100:
                        msg += f"📊 *Last {acc_500['sample']}:*\n"
                        msg += f"✓ {acc_500['correct']} | ✗ {acc_500['wrong']}\n"
                        msg += f"Accuracy: *{acc_500['accuracy']}%*\n\n"
                    
                    if acc_100 and acc_100.get('by_confidence'):
                        msg += "🎯 *By Confidence:*\n"
                        for conf, pct in acc_100['by_confidence'].items():
                            msg += f"{conf}: *{pct}%*\n"
                    
                    tg_send(msg if acc_100 else "⏳ Need 10+ predictions", chat_id)
                
                elif text == "/resetpredictions" and is_admin(chat_id):
                    count = predictions_col.count_documents({})
                    predictions_col.delete_many({})
                    tg_send(
                        f"🗑️ *PREDICTIONS RESET*\n\n"
                        f"Deleted: *{count} predictions*\n"
                        f"Fresh start enabled!",
                        chat_id
                    )
                    print(f"[ADMIN] Reset {count} predictions")
                
                # USER
                elif text == "/start":
                    user = users_col.find_one({"chat_id": str(chat_id)})
                    if user and user.get("active"):
                        tg_send("🎰 *WINGO BOT*\n✅ Active\n/help", chat_id)
                    else:
                        tg_send(f"🎰 *WINGO BOT*\n⚠️ Not authorized\n\nID: `{chat_id}`", chat_id)
                
                elif text == "/help":
                    msg = "📚 *COMMANDS*\n\n/stats\n/mychatid"
                    if is_admin(chat_id):
                        msg += "\n\n👑 *ADMIN*\n/adduser {id}\n/removeuser {id}\n/listusers\n/accuracy\n/resetpredictions"
                    tg_send(msg, chat_id)
                
                elif text == "/stats":
                    count = col.count_documents({})
                    pred_count = predictions_col.count_documents({})
                    users_count = users_col.count_documents({"active": True})
                    acc = get_prediction_accuracy(100)
                    
                    msg = f"📊 *STATS*\n\n🎲 Records: *{count}*\n🔮 Predictions: *{pred_count}*\n👥 Users: *{users_count}*"
                    if acc:
                        msg += f"\n🎯 Accuracy: *{acc['accuracy']}%*"
                    tg_send(msg, chat_id)
                
                elif text == "/mychatid":
                    tg_send(f"🆔 *YOUR ID*\n\n`{chat_id}`", chat_id)
        
        except Exception as e:
            print(f"[CMD ERROR] {e}")
        
        time.sleep(2)

# =====================================================
# 🚀 MONITOR
# =====================================================
async def monitor(page):
    print("[MONITOR] Starting")
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
            
            # Track prediction
            if last_prediction:
                was_correct = last_prediction["predicted"] == side
                predictions_col.insert_one({
                    "period": period,
                    "predicted": last_prediction["predicted"],
                    "actual": side,
                    "correct": was_correct,
                    "confidence_level": last_prediction["confidence"],
                    "probability": last_prediction["probability"],
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
            global last_cache_time
            last_cache_time = 0
            
            if side == current_side:
                current_streak += 1
            else:
                if current_streak >= 3 and current_side:
                    emoji_prev = "🔴" if current_side == "Big" else "🔵"
                    emoji_new = "🔵" if side == "Small" else "🔴"
                    tg_send(
                        f"💔 *BREAK* 💔\n\n"
                        f"{emoji_prev} Was: *{current_streak}x {current_side.upper()}*\n"
                        f"{emoji_new} Now: *{side.upper()}*"
                    )
                
                current_side = side
                current_streak = 1
                last_alerted_streak = 0
            
            settings = get_settings()
            total = col.count_documents({})
            
            if current_streak >= 3 and current_streak > last_alerted_streak and settings["alerts"]:
                data = get_cached_data()
                results = [x["result"] for x in data]
                
                emoji = "🔴" if current_side == "Big" else "🔵"
                fire = "🔥" * min(current_streak, 5)
                
                msg = (
                    f"{fire} *STREAK* {fire}\n\n"
                    f"{emoji} *{current_streak}x {current_side.upper()}* {emoji}\n\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📊 Records: *{total}*"
                )
                
                if total >= MIN_DATA_FOR_CALC:
                    pred = advanced_prediction_v2(current_side, current_streak, results)
                    if pred:
                        cont_bar = "█" * int(pred["continue"]/10) + "░" * (10-int(pred["continue"]/10))
                        brk_bar = "█" * int(pred["break"]/10) + "░" * (10-int(pred["break"]/10))
                        
                        msg += (
                            f"\n\n📈 *ANALYSIS*\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"✅ Continue: *{pred['continue']}%*\n   {cont_bar}\n\n"
                            f"❌ Break: *{pred['break']}%*\n   {brk_bar}\n\n"
                            f"🎯 Confidence: {pred['confidence']}\n"
                            f"💯 Score: *{pred['score']}/100*"
                        )
                        
                        # Show debug info
                        if pred.get("pattern_boost") or pred.get("momentum_boost"):
                            msg += f"\n📊 Pattern: *{pred.get('pattern_boost', 0)}*"
                            msg += f"\n🔄 Momentum: *{pred.get('momentum_boost', 0)}*"
                        
                        if pred["manipulation_detected"]:
                            msg += "\n⚠️ Rigged detected"
                        
                        # BET ALERT - Now triggers more often
                        if pred["should_alert"] and settings.get("bet_alerts", True):
                            bet_emoji = "🔴" if pred["recommendation"] == "Big" else "🔵"
                            msg += (
                                f"\n\n🎯 *BET RECOMMENDATION*\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"{bet_emoji} *BET ON: {pred['recommendation'].upper()}*\n"
                                f"📊 Probability: *{pred['rec_probability']}%*\n"
                                f"⚡ Confidence: {pred['confidence']}"
                            )
                            
                            # Store prediction
                            last_prediction = {
                                "predicted": pred["recommendation"],
                                "confidence": pred["confidence"],
                                "probability": pred["rec_probability"]
                            }
                        else:
                            # Show why no bet alert
                            msg += f"\n\n⏳ Score too low for bet ({pred['score']}/{MIN_CONFIDENCE_TO_ALERT})"
                
                else:
                    msg += f"\n\n⏳ *COLLECTING*\n{total}/{MIN_DATA_FOR_CALC}"
                
                tg_send(msg)
                last_alerted_streak = current_streak
            
            await asyncio.sleep(CHECK_INTERVAL)
        
        except Exception as e:
            print(f"[MONITOR ERROR] {e}")
            await asyncio.sleep(10)

# =====================================================
# ▶ MAIN
# =====================================================
async def main():
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    await asyncio.sleep(2)
    
    # Send startup message ONLY ONCE
    if not bot_status.get("started"):
        try:
            tg_send(
                "🚀 *WINGO BOT v3.0*\n\n"
                "✨ Advanced prediction engine\n"
                "🎯 Bet recommendations enabled\n"
                "📊 500 prediction history\n\n"
                f"⏱️ {datetime.now().strftime('%H:%M:%S')}"
            )
            bot_status["started"] = True
        except:
            pass
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        
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
        print("\n[STOP]")
    except Exception as e:
        print(f"[FATAL] {e}")
