"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

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


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Output translators (database document -> API shape)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = to_ms(doc.get("created_at"))
    return out


def history_out(entry: dict) -> dict:
    changes = {}
    for field, change in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            change = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        changes[field] = change
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


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
        "history": [history_out(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


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
# Business-rule helpers (R1 - R5)
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


def round_half_up(value, places: int = 2) -> float:
    step = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_end_at(att_day: date, shift_start: str, shift_end: str) -> datetime:
    end = at_ist(att_day, shift_end)
    if shift_end <= shift_start:
        end = end + timedelta(days=1)
    return end


def compute_overtime(punch_out: datetime, shift_end_at: datetime) -> int:
    minutes = int((punch_out - shift_end_at).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


def derived_fields(status: str, punch_in, punch_out, att_day: date, emp: dict) -> dict:
    if status not in PRESENCE:
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    out = {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, at_ist(att_day, emp["shift_start"])),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = compute_overtime(
            punch_out, shift_end_at(att_day, emp["shift_start"], emp["shift_end"])
        )
        out["half_day"] = hours < 4.5
    return out


def invalid(field: Optional[str], msg: str, value):
    loc = ["body", field] if field else ["body"]
    return RequestValidationError([{
        "type": "value_error",
        "loc": loc,
        "msg": msg,
        "input": value,
    }])


# --------------------------------------------------------------------------- #
# Models (request bodies)
# --------------------------------------------------------------------------- #
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


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


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System and employees
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
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [employee_out(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
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
    doc = {
        "emp_code": body.emp_code,
        "date": att_day.isoformat(),
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


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = from_ms(body.punched_at)
    else:
        ts = now_utc().replace(microsecond=0)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        raise invalid("punched_at", "punched_at must be after punch_in and within 24 hours", body.punched_at)

    att_day = date.fromisoformat(rec["date"])
    work_hours = compute_work_hours(rec["punch_in"], ts)
    updates = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, shift_end_at(att_day, emp["shift_start"], emp["shift_end"])),
        "half_day": work_hours < 4.5,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


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


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: date, body: RegularizeIn):
    att_day = date  # the date from the URL; renamed so it is not confused with the date type

    # 1. Find the employee and the record
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_day.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # 2. Work out the final status and punch times, and check the rules
    new_status = body.status or rec["status"]
    if new_status in PRESENCE:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise invalid("punch_in", "a presence status requires punch_in", None)
        if body.punch_in is not None:
            local = new_in.astimezone(IST)
            if attendance_date(local, emp["shift_start"], emp["shift_end"]) != att_day:
                raise invalid("punch_in", "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise invalid("punch_out", "punch_out must be after punch_in and within 24 hours", body.punch_out)
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid("status", "ABSENT and LEAVE cannot have punch times", new_status)
        new_in = None
        new_out = None

    # 3. Compare old and new values; keep only what actually changed
    new_values = {"status": new_status, **derived_fields(new_status, new_in, new_out, att_day, emp)}
    old_values = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    changes = {
        field: {"from": old_values[field], "to": value}
        for field, value in new_values.items()
        if old_values[field] != value
    }
    if not changes:
        raise invalid(None, "the request changes nothing", None)

    # 4. Save only if nobody changed the record since we read it, and append history
    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    guard = {"_id": rec["_id"], "punch_out": rec.get("punch_out")}
    if "history" in rec:
        guard["history"] = rec["history"]
    else:
        guard["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else, please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_days(month: str) -> list:
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    if mon == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, mon + 1, 1)
    return [first + timedelta(days=i) for i in range((next_first - first).days)]


def working_days_count(days: list, joined_on: str) -> int:
    joined = date.fromisoformat(joined_on)
    return sum(1 for d in days if d.weekday() < 5 and d >= joined)


def present_value_expr(weekend_dates: list) -> dict:
    day_value = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    return {
        "$cond": [
            {"$in": ["$date", weekend_dates]},
            0,
            {"$cond": [{"$in": ["$status", list(PRESENCE)]}, day_value, 0]},
        ]
    }


def employee_monthly_pipeline(emp_code: str, days: list) -> list:
    weekend_dates = [d.isoformat() for d in days if d.weekday() >= 5]
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": days[0].isoformat(), "$lte": days[-1].isoformat()},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value_expr(weekend_dates)},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    days = month_days(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, days)))
    if rows:
        stats = rows[0]
    else:
        stats = {"present_days": 0, "leave_days": 0, "late_count": 0,
                 "total_late_minutes": 0, "total_overtime_minutes": 0}
    working_days = working_days_count(days, emp["joined_on"])
    present = Decimal(str(stats["present_days"]))
    if working_days > 0:
        attendance_pct = round_half_up(present * 100 / working_days)
    else:
        attendance_pct = None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present),
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
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
