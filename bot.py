# -*- coding: utf-8 -*-
# Özel saatler: 8,10,12,13,14,16,18,19,21,22,23
# Geniş marj destekli ve DEBUG çıktılarını yazdırır.
# asked.json: sorulmuş ID'ler; havuz biterse sıfırlanır ve başa dönülür.

import os, json, requests, hashlib, re
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

STATE_PATH     = "state.json"     # aynı gün aynı saat tekrarı engeller
QUESTIONS_PATH = "questions.json" # Liste (array) beklenir
ASKED_PATH     = "asked.json"     # sorulmuş soru ID'leri (kalıcı)

IST = ZoneInfo("Europe/Istanbul")

# Saat listesi (TR)
SLOTS = [8, 10, 12, 13, 14, 16, 18, 19, 21, 22, 23]
# Varsayılan tolerans: 50 dk (3000 sn). Workflow'tan SLOT_TOLERANCE_SEC gelirse onu kullanır.
TOLERANCE_SEC = int(os.getenv("SLOT_TOLERANCE_SEC", "3000"))

# Twitter limitleri
MAX_TWEET = 280
MAX_POLL_OPT = 25  # her seçenek <= 25

# ------------ dosya yardımcıları ------------
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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

# ------------ slot / state yardımcıları ------------
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
    if not isinstance(qs, list) or not qs:
        raise RuntimeError("questions.json boş veya liste değil.")
    return qs

def question_id(q):
    # 'id' varsa onu kullan; yoksa metinden kararlı kısa hash üret
    if "id" in q and str(q["id"]).strip():
        return str(q["id"]).strip()
    h = hashlib.sha1()
    h.update(q.get("question","").strip().encode("utf-8"))
    # choices varsa onları da dahil edelim
    if isinstance(q.get("choices"), dict):
        for k in sorted(q["choices"].keys()):
            h.update(k.encode("utf-8"))
            h.update(str(q["choices"][k]).encode("utf-8"))
    else:
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
        delta = (now - slot).total_seconds()  # +: geçmişte, -: gelecekte
        if abs(delta) <= tolerance_sec and not already_posted(state, today, hour):
            # Sıralama: önce yakınlık, eşitse geçmiş/şimdi (delta>=0) tercih
            candidates.append((hour, abs(delta), 0 if delta >= 0 else 1))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[1], x[2]))
    return candidates[0][0]

# ------------ format yardımcıları (choices / options) ------------
_A_E_TAG = re.compile(r'^\s*([A-Ea-e])[\)\.\-:]\s*')

def migrate_old_options_to_choices(q):
    """
    Eski format desteği:
    options: ["A) metin", "B) metin", ...] → choices: {"A":"metin",...}, options: ["A","B","C","D"]
    Zaten choices varsa aynen bırakır.
    """
    if isinstance(q.get("choices"), dict) and q["choices"]:
        # choices var -> options kısa etiketlere normalize et
        order = [k for k in ["A","B","C","D"] if k in q["choices"]] or list(q["choices"].keys())[:4]
        q["options"] = order[:4]
        return q

    opts = q.get("options")
    if not isinstance(opts, list) or not opts:
        return q

    choices = {}
    # "A) metin" yakala, değilse sıradan harf ata
    next_letter_idx = 0
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for item in opts[:4]:
        s = str(item)
        m = _A_E_TAG.match(s)
        if m:
            k = m.group(1).upper()
            v = s[m.end():].strip()
        else:
            k = letters[next_letter_idx]
            v = s.strip()
            next_letter_idx += 1
        choices[k] = v

    q["choices"] = choices
    q["options"] = [k for k in ["A","B","C","D"] if k in choices] or list(choices.keys())[:4]
    return q

def build_poll_text(q):
    """
    Tweet gövdesi: soru + uzun şık metinleri + yönlendirme.
    280 karakteri aşarsa önce şıkları, sonra soruyu kısaltır.
    """
    question = q.get("question","").strip()
    ch = q.get("choices", {}) or {}

    # Sabit A-B-C-D sırası (varsa), yoksa mevcut anahtar sırası
    order = [k for k in ["A","B","C","D"] if k in ch] or list(ch.keys())[:4]

    lines = [question]
    for k in order:
        lines.append(f"{k}) {ch.get(k,'').strip()}")

    lines.append("Cevabınızı ankette işaretleyin.")
    text = "\n".join(lines)

    if len(text) <= MAX_TWEET:
        return text

    # Önce şıkları 50 karaktere indir (kademeli)
    for i in range(1, 1 + len(order)):
        if len("\n".join(lines)) <= MAX_TWEET:
            break
        prefix = lines[i][:3]  # "A) "
        payload = lines[i][3:].strip()
        if len(payload) > 50:
            lines[i] = prefix + payload[:47].rstrip() + "…"

    # Hâlâ uzunsa soruyu 120'ye indir
    if len("\n".join(lines)) > MAX_TWEET:
        qline = lines[0]
        if len(qline) > 120:
            lines[0] = qline[:117].rstrip() + "…"

    # Son kontrol: çok uç vakalarda yine de fazla ise en sondaki yönlendirmeyi kısalt
    if len("\n".join(lines)) > MAX_TWEET:
        lines[-1] = "Cevabı ankette işaretleyin."

    return "\n".join(lines)

def build_poll_options(q):
    """
    Ankete gidecek kısa seçenekler (≤25). Yeni formatta ["A","B","C","D"].
    Eski format gelirse migrate_old_options_to_choices zaten normalize edecek.
    """
    opts = q.get("options") or []
    if not opts and isinstance(q.get("choices"), dict):
        order = [k for k in ["A","B","C","D"] if k in q["choices"]] or list(q["choices"].keys())[:4]
        opts = order
    # Güvenlik: kes
    out = []
    for s in opts[:4]:
        s = str(s).strip()
        if len(s) > MAX_POLL_OPT:
            s = s[:MAX_POLL_OPT-1] + "…"
        out.append(s or "Seçenek")
    # En az 2 şartı:
    if len(out) < 2:
        raise RuntimeError("Anket için en az 2 seçenek gerekli.")
    return out

# ------------ Twitter API ------------
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

    # ------- DEBUG ÇIKTILARI -------
    def delta_min(h):
        slot = now.replace(hour=h, minute=0, second=0, microsecond=0)
        return (now - slot).total_seconds() / 60.0

    print("Now(TR):", now.strftime("%Y-%m-%d %H:%M:%S"), "| TOLERANCE_SEC:", TOLERANCE_SEC)
    print("Slots & Δ(min):",
          ", ".join(f"{h:02d}:{delta_min(h):+.1f}" for h in SLOTS))
    # --------------------------------

    target_hour = choose_target_slot(now, state, TOLERANCE_SEC)

    if target_hour is None:
        print("Uygun slot penceresinde değil; gönderim yapılmadı.")
        return
    else:
        # Seçilen slot için farkı ayrıca yazalım
        chosen_delta = delta_min(target_hour)
        print(f"Seçilen slot: {target_hour:02d}:00 | Δ={chosen_delta:+.1f} dk")

    # Yeni soru seç (gerekirse havuzu sıfırla)
    q, qid = pick_next_unasked()
    if not q:
        print("Yeni soru bulunamadı; havuz sıfırlanıyor ve yeniden deneniyor...")
        reset_asked()
        q, qid = pick_next_unasked()
        if not q:
            print("questions.json boş; gönderim atlandı.")
            return

    # --- Yeni/Eski format normalize ---
    q = migrate_old_options_to_choices(q)

    # Tweet gövdesi + anket seçenekleri
    text = build_poll_text(q)
    opts = build_poll_options(q)  # -> ["A","B","C","D"]

    # Güvenlik logları
    print("DEBUG poll options:", opts)
    if any(len(x) > MAX_POLL_OPT for x in opts):
        raise RuntimeError("Hazırlanan poll seçeneklerinden biri 25 karakteri aşıyor.")

    post_id = post_poll(text, opts, 60)

    # Kayıtlar
    mark_posted(state, today, target_hour)
    asked = read_asked_set()
    asked.add(qid)
    write_asked_set(asked)

    print(f"Posted poll {post_id} for {target_hour:02d}:00 (qid={qid})")

if __name__ == "__main__":
    run()
