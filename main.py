import os
import asyncio
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from playwright.async_api import async_playwright
from flask import Flask

# =====================================================
# 🌐 HEALTH CHECK SERVER (FOR RENDER)
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
TG_CHAT_ID = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
MONGO_URI = os.getenv("MONGO_URI")

print("ENV CHECK:",
      "TG_TOKEN", bool(TG_TOKEN),
      "CHAT_ID", bool(TG_CHAT_ID),
      "MONGO_URI", bool(MONGO_URI))

if not all([TG_TOKEN, TG_CHAT_ID, MONGO_URI]):
    raise Exception("❌ Missing env variables")

# =====================================================
# ⚙️ CONFIG
# =====================================================
WINGO_URL = "https://wingoanalyst.com/#/wingo_1m"
CHECK_INTERVAL = 5
MAX_RECORDS = 300
MIN_DATA_FOR_CALC = 100
MIN_MATCH_SAMPLE = 10

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

# =====================================================
# 📤 TELEGRAM
# =====================================================
def tg_send(text):
    print("[TG] Sending message")
    try:
        requests.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown"
            },
            timeout=10
        )
    except Exception as e:
        print("[TG ERROR]", e)

# =====================================================
# 🗄️ MONGODB
# =====================================================
print("[DB] Connecting to MongoDB...")
try:
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Test connection
    mongo.admin.command('ping')
    print("[DB] Connected successfully")
except Exception as e:
    print(f"[DB ERROR] Failed to connect: {e}")
    raise

db = mongo["wingo_bot"]
col = db["results"]
settings_col = db["settings"]

col.create_index([("period", ASCENDING)], unique=True)
print("[DB] Index ensured")

if not settings_col.find_one({"_id": "global"}):
    settings_col.insert_one({
        "_id": "global",
        "alerts": True,
        "probability": True
    })
    print("[DB] Default settings inserted")

def get_settings():
    return settings_col.find_one({"_id": "global"})

# =====================================================
# 🔄 DB HELPERS
# =====================================================
def trim_db():
    count = col.count_documents({})
    if count > MAX_RECORDS:
        extra = count - MAX_RECORDS
        old = col.find().sort("timestamp", 1).limit(extra)
        col.delete_many({"_id": {"$in": [x["_id"] for x in old]}})
        print(f"[DB] Trimmed {extra} old records")

# =====================================================
# 🧮 ADVANCED CALCULATION
# =====================================================
def advanced_calc(target, streak_len):
    data = list(col.find().sort("timestamp", 1))
    results = [x["result"] for x in data]
    total = len(results)

    print(f"[PROB] Calculating for {streak_len}x {target}, data={total}")

    if total < MIN_DATA_FOR_CALC:
        return None

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

    cont_pct = (continued / matched) * 100
    brk_pct = 100 - cont_pct

    streaks = []
    cur = results[0]
    cnt = 1
    for x in results[1:]:
        if x == cur:
            cnt += 1
        else:
            streaks.append(cnt)
            cur = x
            cnt = 1
    streaks.append(cnt)

    avg_streak = sum(streaks) / len(streaks)
    pressure = streak_len / avg_streak if avg_streak else 1

    sample_strength = min(matched / 30, 1) * 30
    bias_strength = abs(cont_pct - 50) * 0.6

    if pressure < 0.8:
        pressure_score = 20
    elif pressure < 1.1:
        pressure_score = 15
    elif pressure < 1.4:
        pressure_score = 10
    else:
        pressure_score = 5

    recent_slice = results[int(total * 0.7):]
    recent_matches = 0
    recent_continues = 0

    for i in range(len(recent_slice) - streak_len):
        w = recent_slice[i:i + streak_len]
        if all(x == target for x in w):
            recent_matches += 1
            if i + streak_len < len(recent_slice) and recent_slice[i + streak_len] == target:
                recent_continues += 1

    if recent_matches > 0:
        recent_bias = abs((recent_continues / recent_matches) * 100 - 50)
        recency_score = min(recent_bias * 0.4, 20)
    else:
        recency_score = 5

    confidence_score = round(
        sample_strength + bias_strength + pressure_score + recency_score, 1
    )

    if confidence_score >= 80:
        confidence = "Very High"
    elif confidence_score >= 60:
        confidence = "High"
    elif confidence_score >= 40:
        confidence = "Moderate"
    else:
        confidence = "Weak"

    print(
        f"[CONF] score={confidence_score} | "
        f"sample={sample_strength:.1f} "
        f"bias={bias_strength:.1f} "
        f"pressure={pressure_score} "
        f"recency={recency_score:.1f}"
    )

    return (
        round(cont_pct, 2),
        round(brk_pct, 2),
        round(pressure, 2),
        confidence,
        confidence_score,
        matched,
        continued,
        total
    )

# =====================================================
# 📥 SCRAPER HELPERS (FIXED)
# =====================================================
async def bootstrap_history(page):
    """Load history only if DB is empty or has very few records"""
    existing_count = col.count_documents({})
    
    if existing_count >= 50:
        print(f"[BOOTSTRAP] Skipping - DB already has {existing_count} records")
        return
    
    print("[BOOTSTRAP] Loading history from page...")

    rows = await page.query_selector_all(
        "div[style*='display: flex'][style*='row']"
    )

    records = []
    inserted = 0
    skipped = 0

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

            # Check if already exists
            if col.find_one({"period": period}):
                skipped += 1
                continue

            records.append({
                "period": period,
                "result": result,
                "timestamp": datetime.now(timezone.utc)
            })
        except Exception as e:
            print(f"[BOOTSTRAP ERROR] Row parse failed: {e}")

    # Insert one by one to handle duplicates gracefully
    for record in records:
        try:
            col.insert_one(record)
            inserted += 1
        except DuplicateKeyError:
            skipped += 1
            continue

    print(f"[BOOTSTRAP] Complete: {inserted} inserted, {skipped} skipped")

async def extract_latest(page):
    rows = await page.query_selector_all(
        "div[style*='display: flex'][style*='row']"
    )

    for r in rows:
        try:
            text = await r.inner_text()
            parts = [p.strip() for p in text.split("\n") if p.strip()]
            if len(parts) < 3:
                continue

            period = parts[0].replace("*", "")
            result = parts[2]

            if result in ("Big", "Small"):
                return period, result
        except Exception as e:
            print(f"[SCRAPER ERROR] {e}")
            continue

    return None

# =====================================================
# 🤖 TELEGRAM COMMAND LISTENER
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
                text = u.get("message", {}).get("text", "")

                if not text:
                    continue

                print("[TG CMD]", text)

                if text == "/start":
                    tg_send("🤖 *Wingo Bot Active*\n\n/help - Commands\n/stats - Statistics\n/usersetting - Settings")

                elif text == "/help":
                    tg_send(
                        "📋 *Available Commands:*\n\n"
                        "/stats - View records\n"
                        "/usersetting - View settings\n"
                        "/alerts on|off - Toggle alerts\n"
                        "/probability on|off - Toggle probability"
                    )

                elif text == "/stats":
                    count = col.count_documents({})
                    tg_send(f"📊 *Database Records:* {count}")

                elif text == "/usersetting":
                    s = get_settings()
                    tg_send(
                        f"⚙️ *CURRENT SETTINGS*\n\n"
                        f"🔔 Alerts: *{'ON' if s['alerts'] else 'OFF'}*\n"
                        f"📊 Probability: *{'ON' if s['probability'] else 'OFF'}*"
                    )

                elif text.startswith("/alerts"):
                    val = "on" in text.lower()
                    settings_col.update_one({"_id": "global"}, {"$set": {"alerts": val}})
                    tg_send(f"🔔 Alerts: *{'ON' if val else 'OFF'}*")

                elif text.startswith("/probability"):
                    val = "on" in text.lower()
                    settings_col.update_one({"_id": "global"}, {"$set": {"probability": val}})
                    tg_send(f"📊 Probability: *{'ON' if val else 'OFF'}*")

        except Exception as e:
            print("[TG CMD ERROR]", e)

        time.sleep(2)

# =====================================================
# 🚀 MAIN MONITOR
# =====================================================
async def monitor(page):
    print("[MONITOR] Starting monitor loop")
    bot_status["running"] = True

    current_side = None
    current_streak = 0
    last_alerted_streak = 0

    while True:
        try:
            res = await extract_latest(page)
            if not res:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            period, side = res
            
            # Check if already exists
            if col.find_one({"period": period}):
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Insert new record
            try:
                col.insert_one({
                    "period": period,
                    "result": side,
                    "timestamp": datetime.now(timezone.utc)
                })
                bot_status["last_update"] = datetime.now(timezone.utc)
                print(f"[DATA] {period} | {side}")
            except DuplicateKeyError:
                print(f"[DATA] Duplicate skipped: {period}")
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            trim_db()

            if side == current_side:
                current_streak += 1
            else:
                if current_streak >= 3:
                    tg_send(
                        f"❌ *Streak Broken*\n\n"
                        f"Previous: *{current_streak}x {current_side.upper()}*\n"
                        f"New: *{side.upper()}*"
                    )

                current_side = side
                current_streak = 1

            settings = get_settings()
            total = col.count_documents({})

            if (current_streak >= 3 and 
                current_streak > last_alerted_streak and 
                settings["alerts"]):
                
                msg = (
                    f"🔥 *{current_streak}x {current_side.upper()} STREAK* 🔥\n\n"
                    f"📊 Database: *{total} rounds*"
                )

                if settings["probability"]:
                    calc = advanced_calc(current_side, current_streak)
                    if calc:
                        cont, brk, pressure, conf, score, matched, continued, _ = calc
                        broken = matched - continued

                        msg += (
                            f"\n\n*📈 Probability Analysis:*\n"
                            f"Continue: *{cont}%*\n"
                            f"Break: *{brk}%*\n"
                            f"Pressure: *{pressure}×*\n"
                            f"Confidence: *{conf}* ({score}/100)\n"
                            f"Sample: *{matched}* ({continued}✓ / {broken}✗)"
                        )

                tg_send(msg)
                last_alerted_streak = current_streak

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("[MONITOR ERROR]", e)
            bot_status["running"] = False
            await asyncio.sleep(10)
            bot_status["running"] = True

# =====================================================
# ▶ RUN
# =====================================================
async def main():
    # Start Flask FIRST
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Wait for Flask to start
    await asyncio.sleep(2)
    print("[FLASK] Health server running")

    try:
        tg_send("🚀 *Wingo Bot Started*")
    except Exception as e:
        print(f"[TG] Initial message failed: {e}")

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
        print("\n[SHUTDOWN] Bot stopped by user")
        bot_status["running"] = False
    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        bot_status["running"] = False
        raise
