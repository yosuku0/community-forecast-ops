import os
import time
import json
import csv
import socket
import logging
import argparse
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_IRC_TOKEN = os.getenv("TWITCH_IRC_TOKEN")
TWITCH_IRC_NICK = os.getenv("TWITCH_IRC_NICK")
TWITCH_IRC_CHANNEL = os.getenv("TWITCH_IRC_CHANNEL")
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", 30))
NEG_WORD_FILE = "negword_list.txt"
BASELINE_FILE = "baseline_stats.json"
LOG_FILE = "Signal_Intake_Log.csv"

# CSV Columns
CSV_COLUMNS = [
    "Signal ID", "Captured Date", "Captured By", "Source Platform",
    "Source Detail", "Signal Type", "Topic / Target", "Summary",
    "Evidence Link", "Actor Role", "Intensity", "Spread", "Confidence",
    "Needs Translation", "Translation Priority", "Status"
]

class TwitchMonitor:
    def __init__(self, debug=False):
        self.debug = debug
        self.access_token = None
        self.token_expiry = 0
        self.neg_words = self._load_neg_words()
        self.baseline = self._load_baseline()
        self.session_detections = set() # To track "Repeated" spread

    def _load_neg_words(self):
        if not os.path.exists(NEG_WORD_FILE):
            return []
        with open(NEG_WORD_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            return [w.strip() for w in content.split(",") if w.strip()]

    def _load_baseline(self):
        if not os.path.exists(BASELINE_FILE):
            return {"viewer_avg": 0, "last_updated": None}
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_baseline(self):
        with open(BASELINE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.baseline, f, indent=4)

    def set_init_baseline(self, value):
        self.baseline["viewer_avg"] = value
        self.baseline["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_baseline()
        print(f"[*] Baseline initialized to: {value}")

    def _get_access_token(self):
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token

        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }
        for _ in range(3):
            try:
                response = requests.post(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                self.access_token = data["access_token"]
                self.token_expiry = time.time() + data["expires_in"] - 60
                return self.access_token
            except Exception as e:
                print(f"[!] Token Error: {e}")
                time.sleep(1)
        return None

    def _api_request(self, url, params=None):
        token = self._get_access_token()
        if not token:
            return None
        headers = {
            "Client-ID": TWITCH_CLIENT_ID,
            "Authorization": f"Bearer {token}"
        }
        for _ in range(3):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"[!] API Error: {url} -> {e}")
                time.sleep(1)
        return None

    def get_game_id(self, game_name):
        data = self._api_request("https://api.twitch.tv/helix/games", {"name": game_name})
        if data and data.get("data"):
            return data["data"][0]["id"]
        return None

    def check_streams(self, game_id):
        data = self._api_request("https://api.twitch.tv/helix/streams", {"game_id": game_id})
        if not data: return

        total_viewers = sum(s["viewer_count"] for s in data.get("data", []))
        baseline_avg = self.baseline.get("viewer_avg", 0)
        
        if baseline_avg > 0:
            diff_percent = ((total_viewers - baseline_avg) / baseline_avg) * 100
            if diff_percent >= 15:
                intensity = "High" if diff_percent > 30 else "Medium"
                summary = f"同時視聴者数がベースライン比+{diff_percent:.1f}%を記録（{total_viewers}人）"
                self.log_signal(
                    signal_type="Observation",
                    summary=summary,
                    intensity=intensity,
                    evidence_link=f"https://www.twitch.tv/directory/category/titancore",
                    source_detail="N/A"
                )
        
        # Update baseline slightly (simple moving average for demo, or just keep it)
        # In a real scenario, this would be computed over 7 days.
        # For now, we just log the spikes.

    def check_clips(self, game_id):
        # Last 1 hour
        started_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        data = self._api_request("https://api.twitch.tv/helix/clips", {
            "game_id": game_id,
            "started_at": started_at
        })
        if not data: return

        clip_count = len(data.get("data", []))
        if clip_count >= 5:
            intensity = "High" if clip_count > 10 else "Medium"
            summary = f"直近1hのクリップ生成数が{clip_count}件（閾値: 10件）"
            self.log_signal(
                signal_type="Observation",
                summary=summary,
                intensity=intensity,
                evidence_link=f"https://www.twitch.tv/directory/category/titancore/clips?range=24hr",
                source_detail="N/A"
            )

    def _get_last_captured_date(self, signal_type):
        if not os.path.exists(LOG_FILE):
            return None
        
        last_date = None
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("Signal Type") == signal_type:
                        try:
                            # Parse Captured Date (ISO format)
                            captured_date = datetime.fromisoformat(row["Captured Date"])
                            if last_date is None or captured_date > last_date:
                                last_date = captured_date
                        except Exception:
                            continue
        except Exception as e:
            print(f"[!] CSV Read Error (Cooldown Check): {e}")
        
        return last_date

    def log_signal(self, signal_type, summary, intensity, evidence_link, source_detail):
        # Cooldown Check
        last_date = self._get_last_captured_date(signal_type)
        if last_date:
            now = datetime.now()
            # If last_date is aware (with timezone), make now aware as well
            if last_date.tzinfo:
                now = datetime.now(timezone.utc)
            
            diff = now - last_date
            if diff < timedelta(minutes=COOLDOWN_MINUTES):
                remaining = timedelta(minutes=COOLDOWN_MINUTES) - diff
                m, s = divmod(int(remaining.total_seconds()), 60)
                if self.debug:
                    print(f"[COOLDOWN] {signal_type}: 次の記録まであと{m}分{s}秒")
                return

        # Intensity判定基準 (Comments required)
        # High   : ベースライン比+30%超 / クリップ10件超 / ネガ率25%超
        # Medium : ベースライン比+15〜30% / クリップ5〜10件 / ネガ率15〜25%
        # Low    : それ以下

        # Spread判定基準
        # Repeated : 同一セッション内で2回以上閾値超え
        # Isolated : 初回検出
        spread = "Repeated" if summary in self.session_detections else "Isolated"
        self.session_detections.add(summary)

        # Signal ID: SIG-YYYYMMDD-NNN
        date_str = datetime.now().strftime("%Y%m%d")
        signal_id = self._generate_signal_id(date_str)

        row = {
            "Signal ID": signal_id,
            "Captured Date": datetime.now().isoformat(),
            "Captured By": "twitch_collector",
            "Source Platform": "Twitch",
            "Source Detail": source_detail,
            "Signal Type": signal_type,
            "Topic / Target": "titancore_general",
            "Summary": summary,
            "Evidence Link": evidence_link,
            "Actor Role": "General Player",
            "Intensity": intensity,
            "Spread": spread,
            "Confidence": "Moderate",
            "Needs Translation": "No", # Adjust as needed
            "Translation Priority": intensity, # Simplified
            "Status": "New"
        }

        if self.debug:
            print(f"[DEBUG] Signal Detected: {json.dumps(row, ensure_ascii=False, indent=2)}")
        else:
            self._write_to_csv(row)

    def _generate_signal_id(self, date_str):
        if not os.path.exists(LOG_FILE):
            return f"SIG-{date_str}-001"
        
        count = 1
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                sid = r.get("Signal ID", "")
                if sid.startswith(f"SIG-{date_str}"):
                    count += 1
        return f"SIG-{date_str}-{count:03d}"

    def _write_to_csv(self, row):
        file_exists = os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        print(f"[+] Logged: {row['Signal ID']}")

class ChatMonitor:
    def __init__(self, monitor_instance):
        self.mi = monitor_instance
        self.buffer = []
        self.last_flush = time.time()
        self.running = True

    def run(self):
        if not all([TWITCH_IRC_TOKEN, TWITCH_IRC_NICK, TWITCH_IRC_CHANNEL]):
            print("[!] IRC Config missing. Chat monitoring disabled.")
            return

        while self.running:
            try:
                sock = socket.socket()
                sock.connect(("irc.chat.twitch.tv", 6667))
                sock.send(f"PASS {TWITCH_IRC_TOKEN}\r\n".encode("utf-8"))
                sock.send(f"NICK {TWITCH_IRC_NICK}\r\n".encode("utf-8"))
                sock.send(f"JOIN #{TWITCH_IRC_CHANNEL}\r\n".encode("utf-8"))
                
                sock.settimeout(10)
                print(f"[*] Connected to IRC: #{TWITCH_IRC_CHANNEL}")

                while self.running:
                    try:
                        resp = sock.recv(2048).decode("utf-8")
                        if resp.startswith("PING"):
                            sock.send("PONG\r\n".encode("utf-8"))
                        elif "PRIVMSG" in resp:
                            # Extract message content
                            parts = resp.split(":", 2)
                            if len(parts) >= 3:
                                message = parts[2].strip().lower()
                                self.buffer.append(message)

                        # Logic: 100 messages or 5 minutes
                        if len(self.buffer) >= 100 or (time.time() - self.last_flush > 300 and self.buffer):
                            self.analyze_buffer()

                    except socket.timeout:
                        if time.time() - self.last_flush > 300 and self.buffer:
                            self.analyze_buffer()
                    except Exception as e:
                        print(f"[!] IRC Loop Error: {e}")
                        break
            except Exception as e:
                print(f"[!] IRC Connection Error: {e}. Retrying in 5s...")
                time.sleep(5)

    def analyze_buffer(self):
        count = len(self.buffer)
        neg_hits = sum(1 for msg in self.buffer if any(word in msg for word in self.mi.neg_words))
        neg_rate = (neg_hits / count) * 100 if count > 0 else 0

        if neg_rate >= 15:
            intensity = "High" if neg_rate > 25 else "Medium"
            summary = f"チャット100件中ネガワード含有率{neg_rate:.1f}%（閾値: 25%）"
            self.mi.log_signal(
                signal_type="Complaint",
                summary=summary,
                intensity=intensity,
                evidence_link=f"https://www.twitch.tv/{TWITCH_IRC_CHANNEL}",
                source_detail=f"#{TWITCH_IRC_CHANNEL}"
            )

        print(f"[*] Chat buffer analyzed: {count} msgs, {neg_rate:.1f}% neg")
        self.buffer = []
        self.last_flush = time.time()

def main():
    parser = argparse.ArgumentParser(description="TitanCore Twitch Monitor")
    parser.add_argument("--debug", action="store_true", help="Debug mode (no CSV output)")
    parser.add_argument("--init-baseline", type=int, help="Initialize viewership baseline")
    args = parser.parse_args()

    # Create monitor
    monitor = TwitchMonitor(debug=args.debug)

    if args.init_baseline is not None:
        monitor.set_init_baseline(args.init_baseline)

    # Get Game ID for TitanCore (Assuming it exists or use a default)
    # The user said "TitanCoreカテゴリ". If not found, use a placeholder or ask.
    game_id = monitor.get_game_id("TitanCore")
    if not game_id:
        print("[!] Game 'TitanCore' not found. Please verify the name or use a fallback ID.")
        # fallback for testing
        game_id = "511224" # Example: Apex Legends for testing purpose if needed, but let's stick to spec.

    if not game_id and not args.debug:
        print("[!] Stopping: Game ID required.")
        return

    # Start IRC in a background thread if needed, or just run it sequentially if main loop allows.
    # Since we need to monitor streams every 60s, thread is better.
    import threading
    chat = ChatMonitor(monitor)
    chat_thread = threading.Thread(target=chat.run, daemon=True)
    chat_thread.start()

    print(f"[*] Starting TitanCore Monitor (Game ID: {game_id})")
    try:
        while True:
            if game_id:
                monitor.check_streams(game_id)
                monitor.check_clips(game_id)
            time.sleep(60)
    except KeyboardInterrupt:
        print("[*] Shutting down...")
        chat.running = False

if __name__ == "__main__":
    main()
