from flask import Flask, request, jsonify
from datetime import datetime
import uuid

app = Flask(__name__)

# Health check (per Render/hosting)
@app.get("/health")
def health():
    return {"ok": True}, 200

@app.post("/api/reservations")
def reservations():
    data = request.get_json(force=True, silent=True) or {}
    # estrai campi attesi (stringhe/num)
    payload = {
        "id": str(uuid.uuid4()),
        "source": data.get("source", "twilio"),
        "call_sid": data.get("call_sid", ""),
        "name": data.get("name", "").strip(),
        "phone": data.get("phone", "").strip(),
        "date": data.get("date", "").strip(),
        "time": data.get("time", "").strip(),
        "people": data.get("people"),
        "items": data.get("items", []),
        "notes": data.get("notes", "").strip(),
        "received_at": datetime.utcnow().isoformat() + "Z"
    }
    # TODO: qui potresti salvare su DB; per ora stampiamo a log
    print("📥 New reservation:", payload, flush=True)
    return jsonify({"status": "ok", "reservation": payload}), 201

if __name__ == "__main__":
    # avvio locale per test
    app.run(host="0.0.0.0", port=8000, debug=True)
