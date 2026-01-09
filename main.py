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
# 🌐 RENDER HEALTH CHECK (Fixes Port Error)
# =====================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Wingo Bot Active")
    def log_message(self, format, *args): return

def run_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"[RENDER] Health check server started on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check, daemon=True).start()

# =====================================================
# 🔐 ENV LOADING
# =====================================================
print("[BOOT] Starting Wingo Bot...")
load_dotenv()

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("TG_CHAT_ID") or os.getenv("CHAT_ID")
MONGO_URI = os.getenv("MONGO_URI")

print(f"[ENV] TG_TOKEN: {bool(TG_TOKEN)}, ADMIN: {ADMIN_ID}, MONGO: {bool(MONGO_URI)}")

if not all([TG_TOKEN, ADMIN_ID, MONGO_URI]):
    raise Exception("❌ Missing env variables")

# =====================================================
# 🗄️ MONGODB (Fixed SSL)
# =====================================================
print("[DB] Connecting to MongoDB...")
mongo = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo["wingo_bot"]
col = db["results"]
users_col = db["users"]
settings_col = db["settings"]

col.create_index([("period", ASCENDING)], unique=True)
if not users_col.find_one({"chat_id": ADMIN_ID}):
    users_col.insert_one({"chat_id": ADMIN_ID})

# =====================================================
# 🧮 PRO CALCULATION (Multi-Timeframe 5000)
# =====================================================
def advanced_calc(target, streak_len):
    data = list(col.find({}, {"result": 1, "_id": 0}).sort("timestamp", -1).limit(5000))
    if len(data) < 100: return None
    
    results = [x["result"] for x in reversed(data)]
    total = len(results)
    
    current_pattern = results[-streak_len:]
    matched = 0
    continued = 0

    for i in range(len(results) - streak_len - 1):
        if results[i : i + streak_len] == current_pattern:
            matched += 1
            if results[i + streak_len] == target:
                continued += 1

    if matched == 0: return None

    cont_pct = (continued / matched) * 100
    brk_pct = 100 - cont_pct
    
    # Simple Strength Calculation
    score = int(min(100, (brk_pct * 0.8) + (min(matched, 50) * 0.4)))
    conf = "Very High" if score > 80 else "High" if score > 60 else "Moderate"
    
    return round(cont_pct, 2), round(brk_pct, 2), conf, score, matched

# =====================================================
# 📤 BROADCAST & FORMATTING
# =====================================================
def tg_broadcast(text):
    print(f"[LOG] Broadcasting to all users...")
    for user in list(users_col.find()):
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                          json={"chat_id": user["chat_id"], "text": text, "parse_mode": "Markdown"})
        except Exception as e: print(f"[ERR] Broadcast failed for {user['chat_id']}: {e}")

# =====================================================
# 📥 SCRAPER HELPERS (Restored Logs)
# =====================================================
async def bootstrap_history(page):
    print("[BOOTSTRAP] Scraping 100 previous outcomes...")
    rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
    records = []
    for r in rows:
        text = await r.inner_text()
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) >= 3 and parts[2] in ("Big", "Small"):
            period = parts[0].replace("*", "")
            if not col.find_one({"period": period}):
                records.append({"period": period, "result": parts[2], "timestamp": datetime.now(timezone.utc)})
    
    if records:
        col.insert_many(records)
        print(f"[BOOTSTRAP] Successfully added {len(records)} outcomes to DB.")

async def extract_latest(page):
    rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
    if rows:
        text = await rows[0].inner_text()
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) >= 3: return parts[0].replace("*", ""), parts[2]
    return None

# =====================================================
# 🤖 COMMAND LISTENER (Fixed /help)
# =====================================================
def command_listener():
    print("[TG] Command listener active")
    offset = 0
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params={"offset": offset+1, "timeout": 20}).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                msg = u.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if text == "/start":
                    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                                  json={"chat_id": chat_id, "text": "🚀 *Wingo Pro Bot*\nUse /help for commands.", "parse_mode": "Markdown"})
                
                elif text == "/help":
                    help_text = "🆘 *Commands:*\n/stats - Database count\n/adduser [id] - (Admin)\n/listusers - (Admin)"
                    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": help_text, "parse_mode": "Markdown"})

                elif chat_id == ADMIN_ID:
                    if text.startswith("/adduser"):
                        new_user = text.split(" ")[1]
                        users_col.update_one({"chat_id": new_user}, {"$set": {"added": True}}, upsert=True)
                        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": ADMIN_ID, "text": f"✅ Added {new_user}"})
                    elif text == "/listusers":
                        ulist = "\n".join([f"👤 `{u['chat_id']}`" for u in users_col.find()])
                        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": ADMIN_ID, "text": f"👥 *Users:*\n{ulist}", "parse_mode": "Markdown"})
        except: pass
        time.sleep(2)

# =====================================================
# 🚀 MAIN MONITOR
# =====================================================
async def monitor(page):
    print("[MONITOR] Starting scrape loop...")
    cur_side, cur_streak, last_alerted = None, 0, 0
    
    while True:
        try:
            res = await extract_latest(page)
            if res:
                period, side = res
                if not col.find_one({"period": period}):
                    print(f"[DATA] New: {period} | {side}")
                    col.insert_one({"period": period, "result": side, "timestamp": datetime.now(timezone.utc)})
                    
                    if side == cur_side: cur_streak += 1
                    else:
                        if cur_streak >= 3: tg_broadcast(f"❌ *Streak Broken*\nEnded: {cur_streak}x {cur_side.upper()}")
                        cur_side, cur_streak, last_alerted = side, 1, 0
                    
                    if cur_streak >= 3 and cur_streak > last_alerted:
                        calc = advanced_calc(cur_side, cur_streak)
                        if calc:
                            cont, brk, conf, score, samples = calc
                            bar = "🔥" * (score // 10) + "⚪" * (10 - (score // 10))
                            step = 1 if cur_streak == 3 else (2 if cur_streak == 4 else 3)
                            msg = (f"🚨 **PRO SIGNAL** 🚨\n━━━━━━━━━━\n🎯 Target: `{cur_side.upper()}`\n📏 Streak: `{cur_streak}x`\n"
                                   f"📉 Break: `{brk}%`\n🎯 Strength: `{score}/100`\n{bar}\n━━━━━━━━━━\n💰 Suggested: `Step {step}`")
                            tg_broadcast(msg)
                            last_alerted = cur_streak
        except Exception as e: print(f"[MONITOR ERROR] {e}")
        await asyncio.sleep(5)

async def main():
    tg_broadcast("🚀 **Bot Started & Monitoring Live**")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        print("[SCRAPER] Opening URL...")
        await page.goto("https://wingoanalyst.com/#/wingo_1m", timeout=60000)
        await asyncio.sleep(8)
        await bootstrap_history(page)
        await asyncio.gather(monitor(page), asyncio.to_thread(command_listener))

if __name__ == "__main__":
    asyncio.run(main())
