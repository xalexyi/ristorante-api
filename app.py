from flask import Flask, request, jsonify
from datetime import datetime
from uuid import uuid4

app = Flask(__name__)

# In-memory store (va benissimo per la demo; in produzione usa un DB)
sessions = {}

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

@app.get("/health")
def health():
    return jsonify({"ok": True}), 200

# ---- SESSIONI --------------------------------------------------------------

@app.get("/api/sessions/<callSid>")
def get_session(callSid: str):
    s = sessions.get(callSid)
    if not s:
        s = blank_session()
        s["callSid"] = callSid
        sessions[callSid] = s
    return jsonify({"session": s}), 200

@app.put("/api/sessions/<callSid>")
def put_session(callSid: str):
    payload = request.get_json(force=True, silent=True) or {}
    update = payload.get("update") or payload.get("session") or {}

    s = sessions.get(callSid) or blank_session()
    s["callSid"] = callSid

    allowed = ["name", "phone", "date", "time", "people", "items", "notes"]
    for k in allowed:
        v = update.get(k)
        if v is not None and v != "":
            s[k] = v

    sessions[callSid] = s
    return jsonify({"session": s}), 200

# ---- PRENOTAZIONI ----------------------------------------------------------

@app.post("/api/reservations")
def reservations():
    data = request.get_json(force=True, silent=True) or {}

    payload = {
        "id": str(uuid4()),
        "source": data.get("source", "twilio"),
        "call_sid": (data.get("call_sid") or data.get("callSid") or "").strip(),
        "name": (data.get("name") or "").strip(),
        "phone": (data.get("phone") or "").strip(),
        "date": (data.get("date") or "").strip(),
        "time": (data.get("time") or "").strip(),
        "people": data.get("people"),
        "items": data.get("items") or [],
        "notes": (data.get("notes") or "").strip(),
        "received_at": datetime.utcnow().isoformat() + "Z",
    }

    # Qui potresti salvare su DB; per ora stampiamo nei log di Render
    print("New reservation:", payload, flush=True)

    return jsonify({"status": "ok", "reservation": payload}), 201

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
