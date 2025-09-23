import os
from datetime import datetime, date, time as dtime
from typing import List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Time, DateTime,
    ForeignKey, UniqueConstraint, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from sqlalchemy.exc import IntegrityError

# --------- DB CONFIG ----------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL non impostata (Render → Settings → Environment).")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# --------- MODELS ----------
class Restaurant(Base):
    __tablename__ = "restaurants"
    id = Column(Integer, primary_key=True)
    name = Column(String(160), nullable=False)
    timezone = Column(String(64), nullable=False, default="Europe/Rome")
    reservations = relationship("Reservation", back_populates="restaurant")

class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(Integer, primary_key=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id"), nullable=False)
    customer_name = Column(String(120), nullable=False)
    people = Column(Integer, nullable=False)
    day = Column(Date, nullable=False)
    time = Column(Time, nullable=False)
    created_at = Column(DateTime, server_default=text("now()"))
    restaurant = relationship("Restaurant", back_populates="reservations")
    __table_args__ = (
        UniqueConstraint("restaurant_id", "customer_name", "day", "time",
                         name="uq_reservation_unique"),
    )

# --------- SCHEMAS ----------
class ReservationIn(BaseModel):
    restaurant_id: int
    customer_name: str = Field(..., min_length=2, max_length=120)
    people: int = Field(..., ge=1, le=20)
    day: date
    time: dtime
    @field_validator("day")
    @classmethod
    def day_must_be_future(cls, v: date) -> date:
        if v < date.today():
            raise ValueError("La data deve essere oggi o futura.")
        return v

class ReservationOut(BaseModel):
    id: int
    restaurant_id: int
    customer_name: str
    people: int
    day: date
    time: dtime
    created_at: datetime
    class Config:
        from_attributes = True

# --------- APP ----------
app = FastAPI(
    title="Ristorante API",
    version="1.0.1",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)

# ---- ROUTES DIAGNOSTICHE ----
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}

@app.get("/")
def root():
    return {"ok": True, "service": "ristorante-api", "docs": "/docs"}

@app.get("/routes")
def routes():
    # utile per capire cosa è realmente esposto sull'istanza in esecuzione
    return [r.path for r in app.router.routes]

# ---- BUSINESS ROUTES ----
@app.post("/reservations", response_model=ReservationOut)
def create_reservation(payload: ReservationIn, db: Session = Depends(get_db)):
    rec = Reservation(
        restaurant_id=payload.restaurant_id,
        customer_name=payload.customer_name,
        people=payload.people,
        day=payload.day,
        time=payload.time,
    )
    db.add(rec)
    try:
        db.commit()
        db.refresh(rec)
        return rec
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Prenotazione già esistente.")

@app.get("/reservations/{restaurant_id}", response_model=List[ReservationOut])
def list_reservations(restaurant_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(Reservation)
        .filter(Reservation.restaurant_id == restaurant_id)
        .order_by(Reservation.day, Reservation.time)
        .all()
    )
    return rows

@app.delete("/reservations/{reservation_id}")
def delete_reservation(reservation_id: int, db: Session = Depends(get_db)):
    row = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Prenotazione non trovata.")
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted_id": reservation_id}
