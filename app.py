from flask import Flask, request, jsonify
from datetime import datetime, date, time as dtime
import re
from uuid import uuid4
from typing import Dict, Any

app = Flask(__name__)

# =========================
# In-memory storages (demo)
# =========================
SESSIONS: Dict[str, Dict[str, Any]] = {}
RESERVATIONS: Dict[str, Dict[str, Any]] = {}  # key: reservation_id
# Idempotency: (restaurant_id, call_sid) -> reservation_id
RES_INDEX: Dict[str, str] = {}

# Active call "locks" per restaurant (rate limiting / backpressure)
ACTIVE: Dict[int, Dict[str, float]] = {}  # {rid: {call_sid: expires_epoch}}
MAX_PARALLEL_PER_RESTAURANT = 3
DEFAULT_TTL_SECONDS = 300

# =========================
# Multi-tenant configuration
# =========================
RESTAURANTS: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "Haru Asian Fusion Restaurant",
        "timezone": "Europe/Rome",
        "min_people": 1,
        "max_people": 12,
        "closed_days": {"2025-12-25", "2025-12-26"},
        # opening windows optional, e.g. [("12:00","15:00"),("19:00","23:00")]
        "opening_hours": [("12:00","15:00"),("19:00","23:00")]
    }
    # aggiungi qui altri ristoranti...
}

# =========================
# Helpers
# =========================
def now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def cleanup_expired(rid: int, now_ts: float):
    bucket = ACTIVE.get(rid, {})
    expired = [sid for sid, exp in bucket.items() if exp <= now_ts]
    for sid in expired:
        bucket.pop(sid, None)
    if not bucket:
        ACTIVE.pop(rid, None)

def is_valid_date(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""))

def is_valid_time(s: str) -> bool:
    return bool(re.fullmatch(r"\d{2}:\d{2}", s or ""))

def within_opening(t: str, windows):
    """t 'HH:MM', windows list of tuples (start,end) 'HH:MM'"""
    if not windows:
        return True
    try:
        hh, mm = map(int, t.split(":"))
        tt = hh * 60 + mm
        for start, end in windows:
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            if sh*60+sm <= tt <= eh*60+em:
                return True
        return False
    except Exception:
        return False

def normalize_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Accetta alias multipli da n8n e normalizza i campi."""
    rid = data.get("restaurant_id") or data.get("restaurantId")
    rid = int(rid) if str(rid).isdigit() else None
    name = (data.get("customer_name") or data.get("name") or "").strip()
    phone = (data.get("customer_phone") or data.get("phone") or "").strip()
    call_sid = (data.get("call_sid") or data.get("callSid") or "").strip()
    source = (data.get("source") or "twilio").strip()
    notes = (data.get("notes") or "").strip()
    people = data.get("people")
    try:
        people = int(people) if people is not None else None
    except Exception:
        people = None
    date_s = (data.get("date") or "").strip()
    time_s = (data.get("time") or "").strip()
    tz = (data.get("tz") or data.get("timezone") or RESTAURANTS.get(rid,{}).get("timezone") or "Europe/Rome").strip()
    return {
        "restaurant_id": rid,
        "name": name,
        "phone": phone,
        "date": date_s,
        "time": time_s,
        "people": people,
        "notes": notes,
        "source": source,
        "call_sid": call_sid,
        "tz": tz
    }

# =========================
# Health
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_utc_iso()}), 200

# =========================
# Internal: locks (rate limit)
# =========================
@app.post("/api/internal/locks/acquire")
def acquire_lock():
    payload = request.get_json(force=True, silent=True) or {}
    rid = payload.get("restaurant_id")
    call_sid = (payload.get("call_sid") or "").strip()
    ttl = int(payload.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
    if not rid or not call_sid:
        return jsonify({"allowed": False, "error": "missing restaurant_id or call_sid", "maximum": MAX_PARALLEL_PER_RESTAURANT, "current": 0}), 400
    rid = int(rid)
    now_ts = datetime.utcnow().timestamp()
    cleanup_expired(rid, now_ts)
    bucket = ACTIVE.setdefault(rid, {})
    # idempotent renew
    if call_sid in bucket:
        bucket[call_sid] = now_ts + ttl
        return jsonify({"allowed": True, "current": len(bucket), "maximum": MAX_PARALLEL_PER_RESTAURANT}), 200
    if len(bucket) >= MAX_PARALLEL_PER_RESTAURANT:
        return jsonify({"allowed": False, "current": len(bucket), "maximum": MAX_PARALLEL_PER_RESTAURANT}), 200
    bucket[call_sid] = now_ts + ttl
    return jsonify({"allowed": True, "current": len(bucket), "maximum": MAX_PARALLEL_PER_RESTAURANT}), 200

@app.post("/api/internal/locks/release")
def release_lock():
    payload = request.get_json(force=True, silent=True) or {}
    rid = payload.get("restaurant_id")
    call_sid = (payload.get("call_sid") or "").strip()
    if not rid or not call_sid:
        return jsonify({"allowed": True, "current": 0, "maximum": MAX_PARALLEL_PER_RESTAURANT}), 200
    rid = int(rid)
    now_ts = datetime.utcnow().timestamp()
    cleanup_expired(rid, now_ts)
    bucket = ACTIVE.get(rid, {})
    bucket.pop(call_sid, None)
    current = len(bucket)
    if current == 0 and rid in ACTIVE:
        ACTIVE.pop(rid, None)
    return jsonify({"allowed": True, "current": current, "maximum": MAX_PARALLEL_PER_RESTAURANT}), 200

# =========================
# Reservations (public)
# =========================
def validate_reservation(p: Dict[str, Any]) -> Dict[str, Any]:
    rid = p["restaurant_id"]
    if not rid or rid not in RESTAURANTS:
        return {"ok": False, "error": "invalid restaurant_id"}
    rconf = RESTAURANTS[rid]
    if not p["name"]:
        return {"ok": False, "error": "missing name"}
    if not p["phone"]:
        return {"ok": False, "error": "missing phone"}
    if p["people"] is None or p["people"] < rconf["min_people"] or p["people"] > rconf["max_people"]:
        return {"ok": False, "error": f"people must be between {rconf['min_people']} and {rconf['max_people']}"}
    if not is_valid_date(p["date"]):
        return {"ok": False, "error": "date must be YYYY-MM-DD"}
    if not is_valid_time(p["time"]):
        return {"ok": False, "error": "time must be HH:MM 24h"}
    if p["date"] in rconf.get("closed_days", set()):
        return {"ok": False, "error": "restaurant closed on this date"}
    if not within_opening(p["time"], rconf.get("opening_hours")):
        return {"ok": False, "error": "time outside opening hours"}
    return {"ok": True}

def upsert_reservation(p: Dict[str, Any]) -> Dict[str, Any]:
    # idempotency by (restaurant_id, call_sid)
    key = f"{p['restaurant_id']}::{p.get('call_sid','')}"
    if p.get("call_sid") and key in RES_INDEX:
        rid = RES_INDEX[key]
        res = RESERVATIONS[rid]
        # opzionale: aggiorna con eventuali campi arrivati dopo
        res.update({k: v for k, v in p.items() if v not in (None, "", [])})
        res["updated_at"] = now_utc_iso()
        res["duplicate"] = True
        return res
    # create new
    rid = str(uuid4())
    res = {
        "id": rid,
        "restaurant_id": p["restaurant_id"],
        "source": p["source"],
        "call_sid": p["call_sid"],
        "name": p["name"],
        "phone": p["phone"],
        "date": p["date"],
        "time": p["time"],
        "people": p["people"],
        "notes": p.get("notes",""),
        "tz": p["tz"],
        "received_at": now_utc_iso()
    }
    RESERVATIONS[rid] = res
    if p.get("call_sid"):
        RES_INDEX[key] = rid
    return res

@app.post("/api/public/reservations")
@app.post("/api/reservations")
def create_reservation():
    data = request.get_json(force=True, silent=True) or {}
    payload = normalize_payload(data)
    valid = validate_reservation(payload)
    if not valid["ok"]:
        return jsonify({"ok": False, "error": valid["error"]}), 400
    res = upsert_reservation(payload)
    status = 200 if res.get("duplicate") else 201
    return jsonify({"ok": True, "reservation": res}), status

# =========================
# Minimal sessions (compat)
# =========================
def blank_session():
    return {
        "callSid": "",
        "name": "",
        "phone": "",
        "date": "",
        "time": "",
        "people": None,
        "items": [],
        "notes": ""
    }

@app.get("/api/sessions/<callSid>")
def get_session(callSid: str):
    s = SESSIONS.get(callSid)
    if not s:
        s = blank_session()
        s["callSid"] = callSid
        SESSIONS[callSid] = s
    return jsonify({"session": s}), 200

@app.put("/api/sessions/<callSid>")
def put_session(callSid: str):
    payload = request.get_json(force=True, silent=True) or {}
    update = payload.get("update") or payload.get("session") or {}
    s = SESSIONS.get(callSid) or blank_session()
    s["callSid"] = callSid
    allowed = ["name", "phone", "date", "time", "people", "items", "notes"]
    for k in allowed:
        v = update.get(k)
        if v is not None and v != "":
            s[k] = v
    SESSIONS[callSid] = s
    return jsonify({"session": s}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
