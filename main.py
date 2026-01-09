import os, asyncio, time, requests, threading, certifi
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from playwright.async_api import async_playwright

# =====================================================
# 🌐 RENDER HEALTH CHECK
# =====================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Wingo Pro Multi-User Bot Active")
    def log_message(self, format, *args): return

def run_health_check():
    server = HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_check, daemon=True).start()

# =====================================================
# 🔐 CONFIG & DB
# =====================================================
load_dotenv()
TG_TOKEN = os.getenv("TG_BOT_TOKEN")
ADMIN_ID = os.getenv("TG_CHAT_ID") 
MONGO_URI = os.getenv("MONGO_URI")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

mongo = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo["wingo_bot"]
col = db["results"]
users_col = db["users"]

if ADMIN_ID and not users_col.find_one({"chat_id": ADMIN_ID}):
    users_col.insert_one({"chat_id": ADMIN_ID, "added_at": datetime.now()})

# =====================================================
# 🧮 PRO CALCULATION ENGINE (Multi-Timeframe)
# =====================================================
def get_pro_analysis(target, streak_len):
    # Fetch 5000 rounds (Optimized projection)
    data = list(col.find({}, {"result": 1, "_id": 0}).sort("timestamp", -1).limit(5000))
    if len(data) < 500: return None
    
    all_res = [x["result"] for x in reversed(data)]
    
    def calculate_for_range(limit):
        slice_res = all_res[-limit:]
        current_pattern = slice_res[-streak_len:]
        matched = 0
        continued = 0
        for i in range(len(slice_res) - streak_len - 1):
            if slice_res[i : i + streak_len] == current_pattern:
                matched += 1
                if slice_res[i + streak_len] == target: continued += 1
        return (round((continued/matched)*100, 1), matched) if matched > 0 else (None, 0)

    # Multi-Timeframe Analysis
    short_term_prob, short_samples = calculate_for_range(1000)
    long_term_prob, long_samples = calculate_for_range(5000)

    if short_term_prob is None or long_term_prob is None: return None

    # Calculate Confidence Score (0-100)
    # Bias towards long-term but penalize if short-term contradicts
    avg_prob = (short_term_prob + long_term_prob) / 2
    break_prob = 100 - avg_prob
    
    # Logic: Higher samples + high break % = Higher Score
    score = int(min(100, (break_prob * 0.8) + (min(long_samples, 50) * 0.4)))
    
    return {
        "short_prob": short_term_prob,
        "long_prob": long_term_prob,
        "break_prob": break_prob,
        "score": score,
        "samples": long_samples
    }

# =====================================================
# 📤 ADVANCED RICH FORMATTING
# =====================================================
def broadcast_pro_signal(side, streak, analysis):
    score = analysis['score']
    # 1. Strength Bar
    bar_filled = int(score / 10)
    strength_bar = "🔥" * bar_filled + "⚪" * (10 - bar_filled)
    
    # 2. Martingale Step Suggestion
    step = 1 if streak <= 3 else (2 if streak == 4 else 3)
    
    msg = (
        f"🚨 **PRO SIGNAL DETECTED** 🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **Target:** `{side.upper()}`\n"
        f"📏 **Current Streak:** `{streak}x`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 **Odds Analysis:**\n"
        f"🔹 Last 1k: `{analysis['short_prob']}% Cont.`\n"
        f"🔸 Last 5k: `{analysis['long_prob']}% Cont.`\n"
        f"📉 **Break Chance:** `{analysis['break_prob']}%`\n\n"
        f"🎯 **Signal Strength:** `{score}/100`\n"
        f"{strength_bar}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 **Betting Advice:**\n"
        f"✅ **Action:** `{'WAIT' if score < 40 else 'REVERSAL (BREAK)'}`\n"
        f"💰 **Suggested:** `Step {step}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    for user in list(users_col.find()):
        try:
            requests.post(f"{TG_API}/sendMessage", json={
                "chat_id": user["chat_id"], "text": msg, "parse_mode": "Markdown"
            }, timeout=5)
        except: pass

# =====================================================
# 🤖 ADMIN COMMANDS
# =====================================================
def command_listener():
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset + 1, "timeout": 20}).json()
            for u in r.get("result", []):
                offset = u["update_id"]
                msg = u.get("message", {})
                uid = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                if uid == ADMIN_ID:
                    if text.startswith("/adduser"):
                        target = text.split(" ")[1]
                        users_col.update_one({"chat_id": target}, {"$set": {"added_at": datetime.now()}}, upsert=True)
                        requests.post(f"{TG_API}/sendMessage", json={"chat_id": ADMIN_ID, "text": f"✅ Added `{target}`"})
                    elif text.startswith("/removeuser"):
                        target = text.split(" ")[1]
                        users_col.delete_one({"chat_id": target})
                        requests.post(f"{TG_API}/sendMessage", json={"chat_id": ADMIN_ID, "text": f"❌ Removed `{target}`"})
                    elif text == "/listusers":
                        all_u = "\n".join([f"👤 `{u['chat_id']}`" for u in users_col.find()])
                        requests.post(f"{TG_API}/sendMessage", json={"chat_id": ADMIN_ID, "text": f"👥 **Users:**\n{all_u}", "parse_mode": "Markdown"})
        except: pass
        time.sleep(2)

# =====================================================
# 🚀 MONITOR LOOP
# =====================================================
async def monitor(page):
    cur_side, cur_streak, last_alerted = None, 0, 0
    while True:
        try:
            rows = await page.query_selector_all("div[style*='display: flex'][style*='row']")
            if rows:
                t = await rows[0].inner_text()
                p = [x.strip() for x in t.split("\n") if x.strip()]
                if len(p) >= 3 and p[2] in ("Big", "Small"):
                    period, side = p[0].replace("*", ""), p[2]
                    
                    if not col.find_one({"period": period}):
                        col.insert_one({"period": period, "result": side, "timestamp": datetime.now(timezone.utc)})
                        if side == cur_side: cur_streak += 1
                        else:
                            if cur_streak >= 3:
                                for u in users_col.find(): 
                                    requests.post(f"{TG_API}/sendMessage", json={"chat_id": u["chat_id"], "text": f"✅ **Streak Ended** at {cur_streak}x"})
                            cur_side, cur_streak, last_alerted = side, 1, 0
                        
                        if cur_streak >= 3 and cur_streak > last_alerted:
                            analysis = get_pro_analysis(cur_side, cur_streak)
                            if analysis:
                                broadcast_pro_signal(cur_side, cur_streak, analysis)
                                last_alerted = cur_streak
        except Exception as e: print(f"Monitor Err: {e}")
        await asyncio.sleep(5)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = await browser.new_page()
        await page.goto("https://wingoanalyst.com/#/wingo_1m", timeout=60000)
        await asyncio.gather(monitor(page), asyncio.to_thread(command_listener))

if __name__ == "__main__":
    asyncio.run(main())
