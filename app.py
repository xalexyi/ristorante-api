import os
import re
import pytz
from datetime import datetime, date, time as dtime
from typing import Optional, Dict, Tuple
from collections import deque
from dateutil import parser as dtparser

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field, field_validator

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Time, DateTime, ForeignKey,
    UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

# =========================
# Config / DB
# =========================
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non impostata (Render > Settings > Environment).")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# =========================
# Modelli SQLAlchemy
# =========================

class Restaurant(Base):
    __tablename__ = "restaurants"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    timezone = Column(String(64), nullable=False, default="Europe/Rome")
    # orari semplici d’esempio (HH:MM-HH:MM, comma-separated per giorni 0..6)
    # es: "12:00-15:00,19:00-23:00" per tutti i giorni
    open_hours = Column(String(255), nullable=False, default="12:00-15:00,19:00-23:00")

    reservations = relationship("Reservation", back_populates="restaurant")

class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id"), nullable=False, index=True)
    people = Column(Integer, nullable=False)
    date = Column(Date, nullable=False)
    time = Column(Time, nullable=False)
    customer_name = Column(String(120), nullable=False)
    customer_phone = Column(String(32), nullable=False)
    notes = Column(String(500), nullable=True)
    source = Column(String(64), nullable=False, default="api")
    call_sid = Column(String(128), nullable=True)  # per idempotenza
    created_at = Column(DateTime(timezone=True), nullable=False)

    restaurant = relationship("Restaurant", back_populates="reservations")

    __table_args__ = (
        UniqueConstraint("restaurant_id", "call_sid", name="uq_rest_callsid"),
    )

# =========================
# Pydantic Schemi
# =========================

PHONE_RE = re.compile(r"^\+?[0-9]{6,16}$")

class ReservationIn(BaseModel):
    restaurant_id: int = Field(..., ge=1)
    people: int = Field(..., ge=1, le=20)
    date: str  # ISO "YYYY-MM-DD" o parole ("oggi","domani") opz. lato n8n
    time: str  # "HH:MM"
    customer_name: str = Field(..., min_length=2, max_length=120)
    customer_phone: str = Field(..., min_length=6, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=500)
    source: Optional[str] = Field(default="api", max_length=64)
    call_sid: Optional[str] = Field(default=None, max_length=128)

    @field_validator("customer_phone")
    @classmethod
    def valid_phone(cls, v: str) -> str:
        if not PHONE_RE.match(v.replace(" ", "")):
            raise ValueError("phone non valido (usa formato internazionale).")
        return v

    @field_validator("time")
    @classmethod
    def valid_time(cls, v: str) -> str:
        try:
            dtime.fromisoformat(v)
        except Exception:
            raise ValueError("time non valido (HH:MM).")
        return v

class ReservationOut(BaseModel):
    id: int
    ok: bool


class RestaurantOut(BaseModel):
    id: int
    name: str
    timezone: str
    open_hours: str

# =========================
# Utilità
# =========================

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def parse_date(v: str, tz: pytz.BaseTzInfo) -> date:
    """
    Accetta 'YYYY-MM-DD'; l'interpretazione di 'oggi/domani' si fa prima in n8n.
    """
    try:
        return date.fromisoformat(v)
    except Exception:
        # fallback: prova a fare parsing "intelligente"
        d = dtparser.parse(v).date()
        return d

def validate_open_hours(restaurant: Restaurant, when_date: date, when_time: dtime) -> None:
    """
    Controllo semplice sugli orari “aperto/chiuso”.
    Esempio: open_hours = "12:00-15:00,19:00-23:00" (tutti i giorni uguale).
    Per produzione: mappa per weekday, giorni festivi ecc.
    """
    ranges = [s.strip() for s in (restaurant.open_hours or "").split(",") if s.strip()]
    if not ranges:
        return
    ok = False
    for r in ranges:
        try:
            st, en = r.split("-")
            st_t = dtime.fromisoformat(st)
            en_t = dtime.fromisoformat(en)
            if st_t <= when_time <= en_t:
                ok = True
                break
        except Exception:
            continue
    if not ok:
        raise HTTPException(status_code=400, detail="orario fuori dalla fascia di apertura")

# =========================
# Rate limit in memoria (semplice)
# =========================

# 60 richieste al minuto per restaurant_id
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))
_rolling: Dict[int, deque] = {}  # restaurant_id -> timestamps unix (secondi)

def rate_limit_check(restaurant_id: int) -> None:
    from time import time as now
    dq = _rolling.setdefault(restaurant_id, deque())
    ts = int(now())
    # rimuovi oltre 60s
    while dq and ts - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="troppo traffico: riprova tra poco")
    dq.append(ts)

# =========================
# App
# =========================

app = FastAPI(title="ristorante-api", version="1.0.0")

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/api/public/restaurants/{rid}", response_model=RestaurantOut)
def get_restaurant(rid: int, db: Session = Depends(get_db)):
    r = db.query(Restaurant).filter(Restaurant.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="restaurant non trovato")
    return RestaurantOut(id=r.id, name=r.name, timezone=r.timezone, open_hours=r.open_hours)

@app.post("/api/public/reservations", response_model=ReservationOut, status_code=201)
def create_reservation(payload: ReservationIn, db: Session = Depends(get_db)):
    # 1) restaurant
    rest = db.query(Restaurant).filter(Restaurant.id == payload.restaurant_id).first()
    if not rest:
        raise HTTPException(status_code=404, detail="restaurant non trovato")

    # 2) rate limit
    rate_limit_check(rest.id)

    # 3) timezone + normalizzazione data
    try:
        tz = pytz.timezone(rest.timezone)
    except Exception:
        tz = pytz.timezone("Europe/Rome")
    d = parse_date(payload.date, tz)
    t = dtime.fromisoformat(payload.time)

    # 4) validazione orari di apertura
    validate_open_hours(rest, d, t)

    # 5) idempotenza (se call_sid presente)
    if payload.call_sid:
        dup = (
            db.query(Reservation)
            .filter(
                Reservation.restaurant_id == rest.id,
                Reservation.call_sid == payload.call_sid
            )
            .first()
        )
        if dup:
            return ReservationOut(id=dup.id, ok=True)

    # 6) crea
    now_aware = datetime.now(tz)
    r = Reservation(
        restaurant_id=rest.id,
        people=payload.people,
        date=d,
        time=t,
        customer_name=payload.customer_name.strip(),
        customer_phone=payload.customer_phone.replace(" ", ""),
        notes=(payload.notes or "").strip() or None,
        source=(payload.source or "api"),
        call_sid=payload.call_sid,
        created_at=now_aware,
    )
    db.add(r)
    try:
        db.commit()
    except Exception as ex:
        db.rollback()
        # se violazione unique -> ritorna l’esistente
        dup2 = (
            db.query(Reservation)
            .filter(
                Reservation.restaurant_id == rest.id,
                Reservation.call_sid == payload.call_sid
            )
            .first()
        )
        if dup2:
            return ReservationOut(id=dup2.id, ok=True)
        raise HTTPException(status_code=500, detail="errore salvataggio")

    db.refresh(r)
    return ReservationOut(id=r.id, ok=True)


# =========================
# Init helper (solo dev)
# Esegui una volta in locale per creare schema + ristorante demo.
# Su Render usa migrazioni reali appena possibile.
# =========================
def _bootstrap():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # se non esiste Haru (id=1), crealo
        ex = db.query(Restaurant).filter(Restaurant.id == 1).first()
        if not ex:
            demo = Restaurant(
                id=1,
                name="Haru Asian Fusion Restaurant",
                timezone="Europe/Rome",
                open_hours="12:00-15:00,19:00-23:00",
            )
            db.add(demo)
            db.commit()
    finally:
        db.close()

if os.environ.get("BOOTSTRAP", "false").lower() == "true":
    _bootstrap()
