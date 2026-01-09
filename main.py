import os
import asyncio
import time
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from pathlib import Path

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from playwright.async_api import async_playwright

# =====================================================
# 🌐 RENDER HEALTH CHECK SERVER (Fixes Port Binding Error)
# =====================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Wingo Bot is Running")
    def log_message(self, format, *args):
        return

def run_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"[RENDER] Health check server started on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check, daemon=True).start()

# =====================================================
# 🔐 ENV LOADING (BULLETPROOF)
# =====================================================
print("[BOOT] Starting Wingo Bot...")

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
    print("[ENV] .env loaded")

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
# 🗄️ MONGODB (Fixes SSL Handshake Error)
# =====================================================
print("[DB] Connecting to MongoDB...")
mongo = MongoClient(
    MONGO_URI,
    tlsCAFile=certifi.where() # CRITICAL FIX
)
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
# 🧮 ADVANCED CALCULATION (ORIGINAL LOGIC RESTORED)
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

    # 🔥 ADVANCED CONFIDENCE MODEL
    sample_strength = min(matched / 30, 1) * 30
    bias_strength = abs(cont_pct - 50) * 0.6 

    if pressure < 0.8: pressure_score = 20
    elif pressure < 1.1: pressure_score = 15
    elif pressure < 1.4: pressure_score = 10
    else: pressure_score = 5

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

    confidence_score = round(sample_strength + bias_strength + pressure_score + recency_score, 1)

    if confidence_score >= 80: confidence = "Very High"
    elif confidence_score >= 60: confidence = "High"
    elif confidence_score >= 40: confidence = "Moderate"
    else: confidence = "Weak"

    print(f"[CONF] score={confidence_score} | samples={matched}")

    return (
        round(cont_pct, 2), round(brk_pct, 2), round(pressure, 2),
        confidence, confidence_score, matched, continued, total
    )

# =====================================================
# 📥 SCRAPER HELPERS
# =====================================================
async def bootstrap_history(page):
    print("[BOOTSTRAP] Clearing DB and loading history...")
    col.delete_many({})
    rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
    records = []
    for r in rows:
        text = await r.inner_text()
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) < 3: continue
        period = parts[0].replace("*", "")
        result = parts[2]
        if result not in ("Big", "Small"): continue
        records.append({
            "period": period,
            "result": result,
            "timestamp": datetime.now(timezone.utc)
        })
    records.sort(key=lambda x: x["period"])
    if records: col.insert_many(records)
    print(f"[BOOTSTRAP] Loaded {len(records)} records")

async def extract_latest(page):
    rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
    for r in rows:
        text = await r.inner_text()
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) < 3: continue
        period = parts[0].replace("*", "")
        result = parts[2]
        if result in ("Big", "Small"): return period, result
    return None

# =====================================================
# 🤖 TELEGRAM COMMAND LISTENER
# =====================================================
def command_listener():
    print("[TG] Command listener started")
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset + 1, "timeout": 30}).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                text = u.get("message", {}).get("text", "")
                if text == "/start":
                    tg_send("🤖 *Wingo Bot Started*\n/help\n/stats\n/usersetting")
                elif text == "/help":
                    tg_send("/stats\n/usersetting\n/alerts on|off\n/probability on|off")
                elif text == "/stats":
                    tg_send(f"📊 Records: *{col.count_documents({})}*")
                elif text == "/usersetting":
                    s = get_settings()
                    tg_send(f"⚙️ *SETTINGS*\nAlerts: {'ON' if s['alerts'] else 'OFF'}\nProb: {'ON' if s['probability'] else 'OFF'}")
                elif text.startswith("/alerts"):
                    val = "on" in text
                    settings_col.update_one({"_id": "global"}, {"$set": {"alerts": val}})
                    tg_send(f"🔔 Alerts {'ON' if val else 'OFF'}")
                elif text.startswith("/probability"):
                    val = "on" in text
                    settings_col.update_one({"_id": "global"}, {"$set": {"probability": val}})
                    tg_send(f"📊 Probability {'ON' if val else 'OFF'}")
        except Exception as e:
            print("[TG CMD ERROR]", e)
        time.sleep(2)

# =====================================================
# 🚀 MAIN MONITOR
# =====================================================
async def monitor(page):
    print("[MONITOR] Starting monitor loop")
    current_side, current_streak, last_alerted_streak = None, 0, 0

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

            col.insert_one({"period": period, "result": side, "timestamp": datetime.now(timezone.utc)})
            trim_db()
            print(f"[DATA] {period} | {side}")

            if side == current_side:
                current_streak += 1
            else:
                if current_streak >= 3:
                    tg_send(f"❌ *Streak broken*\n\nLast streak: *{current_streak}x {current_side.upper()}*")
                current_side, current_streak = side, 1
                last_alerted_streak = 0

            settings = get_settings()
            total = col.count_documents({})

            if (current_streak >= 3 and current_streak > last_alerted_streak and settings["alerts"]):
                msg = f"🔥🔥 *{current_streak}x {current_side.upper()} IN A ROW* 🔥🔥\n\n📊 History: *{total} rounds*"
                if settings["probability"]:
                    calc = advanced_calc(current_side, current_streak)
                    if calc:
                        cont, brk, press, conf, score, matched, continued, total = calc
                        broken = matched - continued
                        msg += (f"\n\n➡️ Continue: *{cont}%*\n➡️ Break: *{brk}%*\n📈 Pressure: *{press}×*\n🎯 Confidence: *{conf}*\n📊 Samples: *{matched}* ({continued}C:{broken}B)")
                tg_send(msg)
                last_alerted_streak = current_streak

            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            print("[MONITOR ERROR]", e)
            await asyncio.sleep(5)

# =====================================================
# ▶ RUN
# =====================================================
async def main():
    tg_send("🚀 *Wingo Bot Started & Monitoring Live*")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        await page.goto(WINGO_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        await bootstrap_history(page)
        await asyncio.gather(monitor(page), asyncio.to_thread(command_listener))

if __name__ == "__main__":
    asyncio.run(main())
