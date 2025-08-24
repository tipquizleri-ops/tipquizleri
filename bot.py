# -*- coding: utf-8 -*-
# İstenen saatler: 8,10,12,13,14,16,18,20,21,22,23
# Geniş marj: SLOT_TOLERANCE_SEC ile (varsayılan ±60 dk).
# asked.json: sorulmuş ID'leri tutar. Havuz biterse asked.json sıfırlanır ve başa dönülür.

import os, json, requests, hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from requests_oauthlib import OAuth1

API_URL = "https://api.twitter.com/2/tweets"

# ENV (GitHub Secrets)
API_KEY       = os.getenv("X_API_KEY")
API_SECRET    = os.getenv("X_API_SECRET")
ACCESS_TOKEN  = os.getenv("X_ACCESS_TOKEN")
ACCESS_SECRET = os.getenv("X_ACCESS_SECRET")
AUTH = OAuth1(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)

STATE_PATH    = "state.json"   # aynı gün aynı saat tekrarı engeller
QUESTIONS_PATH= "questions.json"
ASKED_PATH    = "asked.json"   # sorulmuş soru ID'leri (kalıcı)

IST = ZoneInfo("Europe/Istanbul")

# Saat listesi (TR)
SLOTS = [8, 10, 12, 13, 14, 16, 18, 20, 21, 22, 23]
TOLERANCE_SEC = int(os.getenv("SLOT_TOLERANCE_SEC", "3600"))  # ±60 dk (3600 sn)

# ------------ yardımcılar ------------
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
    if len(state["posted"]) > 365:
        state["posted"] = state["posted"][-365:]
    write_state(state)

def load_questions():
    qs = read_json(QUESTIONS_PATH, [])
    if not qs:
        raise RuntimeError("questions.json boş.")
    return qs

def question_id(q):
    # 'id' varsa onu kullan; yoksa metinden kararlı kısa hash üret
    if "id" in q and str(q["id"]).strip():
        return str(q["id"]).strip()
    h = hashlib.sha1()
    h.update(q.get("question","").strip().encode("utf-8"))
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

def choose_target_slot(now, state, tolerance_sec):
    """Geniş marj içinde **henüz postlanmamış** slotlardan 'now'a en yakın olanı seç."""
    today = now.strftime("%Y-%m-%d")
    candidates = []
    for hour in SLOTS:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        delta = (now - slot).total_seconds()  # + ise geçmişte, - ise gelecekte
        if abs(delta) <= tolerance_sec and not already_posted(state, today, hour):
            # Sıralama: önce yakınlık, eşitse geçmiş/şimdi (delta>=0) tercih
            candidates.append((hour, abs(delta), 0 if delta >= 0 else 1))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[1], x[2]))
    return candidates[0][0]

def post_poll(text, options, duration_minutes=60):
    payload = {"text": text, "poll": {"duration_minutes": duration_minutes, "options": options}}
    r = requests.post(API_URL, auth=AUTH, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Poll failed {r.status_code}: {r.text}")
    return r.json()["data"]["id"]

# ------------ ana akış ------------
def run():
    # Env kontrolü
    for k in ("X_API_KEY","X_API_SECRET","X_ACCESS_TOKEN","X_ACCESS_SECRET"):
        if not os.getenv(k):
            raise RuntimeError(f"{k} eksik. GitHub Secrets'a ekleyin.")

    now   = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    state = read_state()

    target_hour = choose_target_slot(now, state, TOLERANCE_SEC)
    if target_hour is None:
        print("Uygun slot penceresinde değil; gönderim yapılmadı.")
        return

    # Yeni soru seç (gerekirse havuzu sıfırla)
    q, qid = pick_next_unasked()
    if not q:
        print("Yeni soru bulunamadı; havuz sıfırlanıyor ve yeniden deneniyor...")
        reset_asked()
        q, qid = pick_next_unasked()
        if not q:
            print("questions.json boş; gönderim atlandı.")
            return

    text_lines = [q["question"]] + q["options"]
    text = "\n".join(text_lines)
    post_id = post_poll(text, q["options"], 60)

    # Kayıtlar
    mark_posted(state, today, target_hour)
    asked = read_asked_set()
    asked.add(qid)
    write_asked_set(asked)

    print(f"Posted poll {post_id} for {target_hour:02d}:00 (qid={qid})")

if __name__ == "__main__":
    run()
