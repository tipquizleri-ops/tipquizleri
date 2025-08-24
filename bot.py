# -*- coding: utf-8 -*-
# 2 saatte bir anket (TR: 08,10,12,14,16,18,20,22).
# Aynı soruyu tekrarlamaz; tüm sorular bitince asked.json'ı SIFIRLAYIP başa döner.

import os, json, requests, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from requests_oauthlib import OAuth1

API_URL = "https://api.twitter.com/2/tweets"

# ENV değişkenleri (GitHub Secrets)
API_KEY = os.getenv("X_API_KEY")
API_SECRET = os.getenv("X_API_SECRET")
ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("X_ACCESS_SECRET")
AUTH = OAuth1(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)

STATE_PATH = "state.json"     # slot bazlı tekrar koruması
QUESTIONS_PATH = "questions.json"
ASKED_PATH = "asked.json"     # sorulan ID'ler (kalıcı)

IST = ZoneInfo("Europe/Istanbul")

# 2 saatte bir: 08..22
SLOTS = list(range(8, 24, 2))  # [8,10,12,14,16,18,20,22]
TOLERANCE_SEC = int(os.getenv("SLOT_TOLERANCE_SEC", "360"))  # ±6 dk

# ---------- yardımcılar ----------
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def read_state():
    return read_json(STATE_PATH, {})

def write_state(s):
    write_json(STATE_PATH, s)

def read_asked_set():
    data = read_json(ASKED_PATH, {"asked": []})
    return set(map(str, data.get("asked", [])))

def write_asked_set(s):
    write_json(ASKED_PATH, {"asked": sorted(list(s))})

def reset_asked():
    write_asked_set(set())
    print("asked.json sıfırlandı; havuz başa alındı.")

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
    qs = read_json(QUESTIONS_PATH, [])
    if not qs:
        raise RuntimeError("questions.json boş.")
    return qs

def question_id(q):
    """
    Öncelik: q['id'] varsa onu kullan.
    Yoksa soru+şık metninden kararlı bir kısa hash üret.
    """
    if "id" in q and str(q["id"]).strip():
        return str(q["id"]).strip()
    h = hashlib.sha1()
    h.update(q.get("question", "").strip().encode("utf-8"))
    for opt in q.get("options", []):
        h.update(str(opt).strip().encode("utf-8"))
    return h.hexdigest()[:16]

def pick_next_unasked():
    asked = read_asked_set()
    for q in load_questions():
        qid = question_id(q)
        if qid not in asked:
            return q, qid
    return None, None

def post_poll(text, options, duration_minutes=60):
    payload = {"text": text, "poll": {"duration_minutes": duration_minutes, "options": options}}
    r = requests.post(API_URL, auth=AUTH, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Poll failed {r.status_code}: {r.text}")
    return r.json()["data"]["id"]

# ---------- ana akış ----------
def run():
    # Zorunlu env kontrolü
    for k in ("X_API_KEY","X_API_SECRET","X_ACCESS_TOKEN","X_ACCESS_SECRET"):
        if not os.getenv(k):
            raise RuntimeError(f"{k} eksik. GitHub Secrets'a ekleyin.")

    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    state = read_state()

    # Slot saatinde ise anket at
    for hour in SLOTS:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if abs((now - slot).total_seconds()) <= TOLERANCE_SEC:
            if already_posted(state, today, hour):
                print("Bu slot daha önce postlanmış, çıkılıyor.")
                return

            # 1) Yeni (sorulmamış) soru bulmaya çalış
            q, qid = pick_next_unasked()
            # 2) Havuz bitmişse asked.json'ı sıfırla ve tekrar dene
            if not q:
                print("Yeni soru bulunamadı; havuz sıfırlanıyor ve başa dönülüyor...")
                reset_asked()
                q, qid = pick_next_unasked()
                if not q:
                    # Bu noktaya sadece questions.json gerçekten boşsa düşer
                    print("questions.json boş görünüyor; gönderim atlandı.")
                    return

            # Tweet metni (soru + şıklar)
            text_lines = [q["question"]] + q["options"]
            text = "\n".join(text_lines)
            post_id = post_poll(text, q["options"], 60)

            # Kayıtlar
            mark_posted(state, today, hour)
            asked = read_asked_set()
            asked.add(qid)
            write_asked_set(asked)

            print(f"Posted poll {post_id} for {hour:02d}:00 (qid={qid})")
            return

    print("Slot dışında çalıştı; gönderim yapılmadı.")

if __name__ == "__main__":
    run()
