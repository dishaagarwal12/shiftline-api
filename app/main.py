"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo import MongoClient

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=2000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)

def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms // 1000 * 1000)

def employee_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_ms(doc.get("created_at"))
    return out

def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": doc.get("history", []),
    }

def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])

@asynccontextmanager
async def lifespan(app):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def at_ist(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def attendance_date(local: datetime, shift_start: str, shift_end: str) -> date:
    day = local.date()
    end_h, end_m = map(int, shift_end.split(":"))
    if shift_end <= shift_start and local.time() < time(end_h, end_m):
        day = day - timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start_at: datetime) -> int:
    seconds = (punch_in - shift_start_at).total_seconds()
    if seconds <= 10 * 60:
        return 0
    return int(seconds // 60)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: date

    @model_validator(mode="after")
    def check_shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = [employee_out(d) for d in db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = now_utc().replace(microsecond=0)
    local = ts.astimezone(IST)
    att_day = attendance_date(local, emp["shift_start"], emp["shift_end"])
    d = att_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, at_ist(att_day, emp["shift_start"])),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise RequestValidationError([{
            "type": "value_error",
            "loc": ["query", "date_from"],
            "msg": "date_from must be on or before date_to",
            "input": date_from.isoformat(),
        }])
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
