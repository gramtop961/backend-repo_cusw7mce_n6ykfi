import os
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from bson import ObjectId

from database import db, create_document, get_documents
from schemas import Appointment as AppointmentSchema

app = FastAPI(title="ILENAI Nail'Z API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Utils
# ----------------------------
class PyObjectId(ObjectId):
    @staticmethod
    def __get_validators__():
        yield PyObjectId.validate

    @staticmethod
    def validate(v):
        if isinstance(v, ObjectId):
            return v
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return ObjectId(v)


def to_utc_naive(dt: datetime) -> datetime:
    # Store as naive UTC to simplify duplicate checks
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


# Simple admin auth using bearer token
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "demo-admin-secret")


def admin_required(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    if token != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


# ----------------------------
# Models
# ----------------------------
class AppointmentCreate(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    email: EmailStr
    phone: str = Field(..., min_length=6, max_length=25)
    datetime_iso: datetime
    location: str = Field("Mourenx")
    notes: Optional[str] = Field(None, max_length=500)


class AppointmentUpdate(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=50)
    last_name: Optional[str] = Field(None, min_length=1, max_length=50)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, min_length=6, max_length=25)
    datetime_iso: Optional[datetime] = None
    location: Optional[str] = None
    status: Optional[str] = Field(None, pattern=r"^(booked|confirmed|done|canceled)$")
    notes: Optional[str] = Field(None, max_length=500)


class AppointmentOut(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: EmailStr
    phone: str
    datetime_iso: datetime
    location: str
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def read_root():
    return {"message": "ILENAI Nail'Z API is running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


@app.get("/api/appointments", response_model=List[AppointmentOut])
def list_appointments(_: bool = Depends(admin_required)):
    docs = db["appointment"].find().sort("datetime_iso", 1)
    result: List[AppointmentOut] = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        result.append(AppointmentOut(**d))
    return result


@app.get("/api/availability")
def check_availability(date: str = Query(..., description="YYYY-MM-DD")):
    try:
        # Match on date by string prefix of ISO date stored
        start = datetime.fromisoformat(date + "T00:00:00")
        end = datetime.fromisoformat(date + "T23:59:59.999999")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    exists = list(db["appointment"].find({
        "datetime_iso": {"$gte": start, "$lte": end},
        "status": {"$ne": "canceled"}
    }, {"datetime_iso": 1}))
    return {"occupied": [e["datetime_iso"].isoformat() for e in exists]}


@app.post("/api/appointments", response_model=AppointmentOut)
def create_appointment(payload: AppointmentCreate):
    # Normalize and prevent duplicates for exact datetime
    dt = payload.datetime_iso
    # Ensure minute precision (round seconds)
    dt = dt.replace(second=0, microsecond=0)

    # Check duplicate
    dup = db["appointment"].find_one({
        "datetime_iso": dt,
        "status": {"$ne": "canceled"}
    })
    if dup:
        raise HTTPException(status_code=409, detail="Ce créneau est déjà réservé. Merci de choisir un autre horaire.")

    doc = AppointmentSchema(
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        phone=payload.phone,
        datetime_iso=dt,
        location=payload.location or "Mourenx",
        notes=payload.notes,
    ).model_dump()

    inserted_id = create_document("appointment", doc)
    saved = db["appointment"].find_one({"_id": ObjectId(inserted_id)})
    saved["id"] = str(saved.pop("_id"))
    return AppointmentOut(**saved)


@app.patch("/api/appointments/{appointment_id}", response_model=AppointmentOut)
def update_appointment(appointment_id: str, payload: AppointmentUpdate, _: bool = Depends(admin_required)):
    try:
        oid = ObjectId(appointment_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid appointment id")

    updates = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if "datetime_iso" in updates and updates["datetime_iso"] is not None:
        updates["datetime_iso"] = updates["datetime_iso"].replace(second=0, microsecond=0)
        # Check duplicate if new datetime collides
        dup = db["appointment"].find_one({
            "_id": {"$ne": oid},
            "datetime_iso": updates["datetime_iso"],
            "status": {"$ne": "canceled"}
        })
        if dup:
            raise HTTPException(status_code=409, detail="Ce créneau est déjà réservé.")

    updates["updated_at"] = datetime.utcnow()

    res = db["appointment"].find_one_and_update(
        {"_id": oid},
        {"$set": updates},
        return_document=True
    )
    if not res:
        raise HTTPException(status_code=404, detail="Appointment not found")

    res["id"] = str(res.pop("_id"))
    return AppointmentOut(**res)


@app.delete("/api/appointments/{appointment_id}")
def delete_appointment(appointment_id: str, _: bool = Depends(admin_required)):
    try:
        oid = ObjectId(appointment_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid appointment id")

    res = db["appointment"].delete_one({"_id": oid})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
