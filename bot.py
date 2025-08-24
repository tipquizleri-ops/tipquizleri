# -*- coding: utf-8 -*-
# 3 gönderi/gün (TR: 10:00, 16:00, 22:00).
# SLOT DIŞINDA normalde gönderim yapmaz; ancak FORCE_POLL_NOW=1 ise hemen bir test anketi atar.

import os, json, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from requests_oauthlib import OAuth1

API_URL = "https://api.twitter.com/2/tweets"

# ENV değişkenleri (GitHub Secrets'tan gelecek)
API_KEY = os.getenv("X_API_KEY")
API_SECRET = os.getenv("X_API_SECRET")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("X_ACCESS_SECRET")

AUTH = OAuth1(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)

STATE_PATH = "state.json"
QUESTIONS_PATH = "questions.json"
IST = ZoneInfo("Europe/Istanbul")

# TR saatleri
SLOTS = [10, 16, 22]  # 10:00, 16:00, 22:00

def read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def write_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)

def already_posted(state, datestr, hour):
    key = f"{datestr}-{hour:02d}"
    return key in state.get("posted", [])

def mark_posted(state, datestr, hour):
    key = f"{datestr}-{hour:02d}"
    state.setdefault("posted", []).append(key)
    if len(state["posted"]) > 300:
        state["posted"] = state["posted"][-300:]
    write_state(state)

def load_questions():
    with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_next_question(state):
    qs = load_questions()
    if not qs:
        raise RuntimeError("questions.json boş.")
    idx = state.get("q_index", 0) % len(qs)
    q = qs[idx]
    state["q_index"] = (idx + 1) % len(qs)  # sırayla, bitince başa dön
    write_state(state)
    return q

def post_poll(text, options, duration_minutes=60):
    payload = {"text": text, "poll": {"duration_minutes": duration_minutes, "options": options}}
    r = requests.post(API_URL, auth=AUTH, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Poll failed {r.status_code}: {r.text}")
    return r.json()["data"]["id"]

def run():
    # Zorunlu env kontrolü
    for k in ("X_API_KEY","X_API_SECRET","X_ACCESS_TOKEN","X_ACCESS_SECRET"):
        if not os.getenv(k):
            raise RuntimeError(f"{k} eksik. GitHub Secrets'a ekleyin.")

    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    state = read_state()

    # --- ACİL TEST: FORCE_POLL_NOW=1 ise hemen bir anket at ve çık ---
    if os.getenv("FORCE_POLL_NOW") == "1":
        q = get_next_question(state)
        text_lines = [q["question"]] + q["options"]
        text = "\n".join(text_lines)
        post_id = post_poll(text, q["options"], 60)
        print(f"Manual test poll posted: {post_id}")
        return
    # -----------------------------------------------------------------

    # Slot saatinde ise anket at
    for hour in SLOTS:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if abs((now - slot).total_seconds()) <= 120:  # ±2 dk esneklik
            if already_posted(state, today, hour):
                print("Bu slot daha önce postlanmış, çıkılıyor.")
                return

            q = get_next_question(state)
            text_lines = [q["question"]] + q["options"]
            text = "\n".join(text_lines)
            post_id = post_poll(text, q["options"], 60)
            mark_posted(state, today, hour)
            print(f"Posted poll {post_id} for {hour:02d}:00")
            return

    # Slot dışında hiçbir şey gönderme
    print("Slot dışında çalıştı; gönderim yapılmadı.")

if __name__ == "__main__":
    run()

