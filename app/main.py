"""Employee Attendance & Analytics API for the HROne trainee assignment."""
import os
import re
import json
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
MAX_EPOCH_MS = 4102444800000
MIN_EPOCH_MS = 100000000000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def mongo_json_safe(value):
    return json.loads(json_util.dumps(value))

def create_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_emp_date_desc")
    attendance.create_index([("status", ASCENDING), ("date", ASCENDING)], name="ix_attendance_status_date")
    attendance.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_emp")


@app.on_event("startup")
def startup():
    create_indexes()


def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        fail422(f"{field} must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail422("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)  # PyMongo returns naive UTC datetimes.
    return int(dt.timestamp() * 1000)


def to_bson_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).replace(microsecond=0, tzinfo=None)


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def now_bson() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def validate_epoch(value):
    if value is None:
        return None
    if type(value) is not int or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        fail422("timestamps must be integer epoch milliseconds in the supported range")
    return value


def local_instant(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(IST).replace(microsecond=0)


def localize_bson(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).astimezone(IST).replace(microsecond=0)
    return dt.astimezone(IST).replace(microsecond=0)


def shift_start_instant(att_date: date, shift_start: str) -> datetime:
    h, m = map(int, shift_start.split(":"))
    return datetime.combine(att_date, time(h, m), tzinfo=IST)


def shift_end_instant(att_date: date, shift_start: str, shift_end: str) -> datetime:
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = time(sh, sm)
    end = time(eh, em)
    end_date = att_date + timedelta(days=1) if end <= start else att_date
    return datetime.combine(end_date, end, tzinfo=IST)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_minutes, end_minutes = sh * 60 + sm, eh * 60 + em
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    start = shift_start_instant(att_date, shift_start)
    seconds = int((punch_in.astimezone(IST) - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def mongo_half_up(expression, places: int = 2):
    """Mongo aggregation expression for non-negative half-up rounding."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, scale]}, 0.5]}}, scale]}


def derived_values(emp: dict, att_date: date, punch_in: Optional[datetime], punch_out: Optional[datetime], status: str):
    if status not in PRESENT_STATUSES:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_date, emp["shift_start"]) if punch_in else 0
    if not punch_out or not punch_in:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = round_half_up(seconds / 3600, 2)
    end = shift_end_instant(att_date, emp["shift_start"], emp["shift_end"])
    overtime_seconds = int((punch_out.astimezone(IST) - end).total_seconds())
    overtime = overtime_seconds // 60 if overtime_seconds >= 1800 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["created_at"] = epoch_ms(result.get("created_at"))
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["punch_in"] = epoch_ms(result.get("punch_in"))
    result["punch_out"] = epoch_ms(result.get("punch_out"))
    result["late_minutes"] = int(result.get("late_minutes") or 0)
    result["overtime_minutes"] = int(result.get("overtime_minutes") or 0)
    result["half_day"] = bool(result.get("half_day", False))
    result["history"] = result.get("history") or []
    for entry in result["history"]:
        entry["at"] = epoch_ms(entry.get("at"))
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                for side in ("from", "to"):
                    entry["changes"][field][side] = epoch_ms(entry["changes"][field].get(side))
    return result


def count_weekdays(start: date, end: date) -> int:
    if end < start:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def month_filter(month: str) -> tuple[date, date, str, str]:
    first, last = parse_month(month)
    return first, last, first.isoformat(), last.isoformat()


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v):
        return validate_epoch(v)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v):
        return validate_epoch(v)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_time(cls, v):
        return validate_epoch(v)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_bson()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    docs = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    instant = local_instant(body.punched_at) if body.punched_at is not None else datetime.now(IST).replace(microsecond=0)
    att_date = attendance_date_for(instant, emp["shift_start"], emp["shift_end"])
    d = att_date.isoformat()
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status,
           "punch_in": instant.astimezone(UTC).replace(tzinfo=None), "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(instant, att_date, emp["shift_start"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES:
        fail422("invalid status")
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        fail422("date_from must be on or before date_to")
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    total = attendance.count_documents(q)
    docs = attendance.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    out_local = local_instant(body.punched_at) if body.punched_at is not None else datetime.now(IST).replace(microsecond=0)
    out_bson = out_local.astimezone(UTC).replace(tzinfo=None)
    out_date = out_local.date().isoformat()
    candidates = list(attendance.find({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out_bson}},
                                      {"_id": 1, "date": 1, "punch_in": 1, "punch_out": 1}).sort("punch_in", DESCENDING).limit(1))
    if not candidates:
        raise HTTPException(status_code=404, detail="no punch-in found before punch-out time")
    rec = candidates[0]
    pin = rec["punch_in"].replace(tzinfo=UTC) if rec["punch_in"].tzinfo is None else rec["punch_in"].astimezone(UTC)
    pout = out_local.astimezone(UTC)
    delta = (pout - pin).total_seconds()
    if delta <= 0 or delta > 24 * 3600:
        fail422("punch-out must be after punch-in and within 24 hours")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance already punched out")
    att_date = parse_date(rec["date"])
    pin_local = localize_bson(rec["punch_in"])
    derived = derived_values(emp, att_date, pin_local, out_local, "PRESENT")
    updated = attendance.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": out_bson, **derived}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(status_code=409, detail="attendance already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_day = parse_date(date)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    original = attendance.find_one({"emp_code": emp_code, "date": date})
    if not original:
        raise HTTPException(status_code=404, detail="attendance record not found")
    old_status = original.get("status")
    status = body.status if body.status is not None else old_status
    pin_ms = body.punch_in if "punch_in" in body.model_fields_set else epoch_ms(original.get("punch_in"))
    pout_ms = body.punch_out if "punch_out" in body.model_fields_set else epoch_ms(original.get("punch_out"))
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            fail422("ABSENT and LEAVE cannot be supplied with punch times")
        pin_ms = pout_ms = None
    elif pin_ms is None:
        fail422("presence status requires punch_in")
    pin = to_bson_utc(pin_ms).replace(tzinfo=UTC) if pin_ms is not None else None
    pout = to_bson_utc(pout_ms).replace(tzinfo=UTC) if pout_ms is not None else None
    if pin is not None:
        pin_local = pin.astimezone(IST)
        expected_day = attendance_date_for(pin_local, emp["shift_start"], emp["shift_end"])
        if expected_day != att_day:
            fail422("punch_in must belong to the attendance date")
    if pout is not None:
        if pin is None or (pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 24 * 3600:
            fail422("punch_out must be after punch_in and within 24 hours")
    derived = derived_values(emp, att_day, pin, pout, status)
    new_values = {"status": status, "punch_in": pin.replace(tzinfo=None) if pin else None,
                  "punch_out": pout.replace(tzinfo=None) if pout else None, **derived}
    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for field in fields:
        old = original.get(field, False if field == "half_day" else 0 if field in ("late_minutes", "overtime_minutes") else None)
        new = new_values[field]
        if field in ("punch_in", "punch_out"):
            old_ms = epoch_ms(old) if isinstance(old, datetime) else None
            new_ms = epoch_ms(new) if isinstance(new, datetime) else None
            if old_ms != new_ms: changes[field] = {"from": old, "to": new}
        elif old != new:
            changes[field] = {"from": old, "to": new}
    if not changes:
        fail422("correction does not change anything")
    history_entry = {"at": now_bson(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    old_history = original.get("history") or []
    updated = attendance.find_one_and_update(
        {"_id": original["_id"], "history": old_history},
        {"$set": new_values, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(status_code=409, detail="attendance was changed concurrently; retry")
    return serialize_attendance(updated)


def monthly_employee_pipeline(emp_code: str, start_s: str, end_s: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last, start_s, end_s = month_filter(month)
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp: raise HTTPException(status_code=404, detail="employee not found")
    working = count_weekdays(max(first, parse_date(emp["joined_on"], "joined_on")), last)
    result = list(attendance.aggregate(monthly_employee_pipeline(emp_code, start_s, end_s)))
    agg = result[0] if result else {}
    present = float(agg.get("present_days", 0))
    pct = round_half_up(present / working * 100, 4) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working,
            "present_days": round_half_up(present, 2), "leave_days": int(agg.get("leave_days", 0)),
            "late_count": int(agg.get("late_count", 0)), "total_late_minutes": int(agg.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)), "attendance_pct": pct}


def department_summary_pipeline(start_s: str, end_s: str, department: Optional[str] = None):
    match = {"joined_on": {"$lte": end_s}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$sum": {"$cond": [{"$eq": ["$logs", None]}, 0, 0]}},
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENT_STATUSES)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$employees"},
            "present_days": mongo_half_up("$present_days", 2),
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, mongo_half_up({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2), None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    _, last, start_s, end_s = month_filter(month)
    items = list(employees.aggregate(department_summary_pipeline(start_s, end_s, department)))
    # Remove departments whose headcount is zero (pipeline starts from eligible employees).
    return {"month": month, "items": [x for x in items if x["headcount"] > 0]}


def leaderboard_pipeline(start_s: str, end_s: str, limit: int, department: Optional[str] = None):
    emp_match = {} if department is None else {"department": department}
    return [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department} if department is not None else {"employee.emp_code": {"$exists": True}}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    _, _, start_s, end_s = month_filter(month)
    return {"month": month, "items": list(attendance.aggregate(leaderboard_pipeline(start_s, end_s, limit, department)))}


def trend_pipeline(department: str, from_s: str, to_s: str):
    start_dt = datetime.combine(parse_date(from_s), time.min)
    end_dt = datetime.combine(parse_date(to_s), time.min)
    return [
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$date"}}}},
        {"$group": {"_id": "$day",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}, "weekday": {"$dayOfWeek": "$_id"}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, "is_working_day": {"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, mongo_half_up({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": mongo_half_up("$present_count", 2), "late_count": 1, "attendance_rate": 1, "moving_avg_7d": mongo_half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start: fail422("to must be on or after from")
    if (end - start).days + 1 > 92: fail422("date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="department not found")
    items = list(attendance.aggregate(trend_pipeline(department, start.isoformat(), end.isoformat())))
    # $densify emits rows only if at least one input row exists; seed the range through a one-row pipeline when empty.
    if not items:
        items = list(employees.aggregate([
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "date": {"$literal": start.isoformat()}}},
            {"$set": {"_id": {"$dateFromString": {"dateString": "$date"}}, "present_count": 0, "late_count": 0}},
            {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start,time.min), datetime.combine(end + timedelta(days=1),time.min)]}}},
            {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}, "weekday": {"$dayOfWeek": "$_id"}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_id"},2]},{"$lte": [{"$dayOfWeek": "$_id"},6]}]}}},
            {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},{"$count":"n"}], "as":"hc"}},
            {"$set": {"headcount": {"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]}, "attendance_rate": {"$cond":[{"$and":["$is_working_day",{"$gt":[{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},0]}]},0.0,None]}}},
            {"$setWindowFields": {"sortBy":{"_id":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
            {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":{"$literal":0},"late_count":{"$literal":0},"attendance_rate":1,"moving_avg_7d":mongo_half_up("$moving_avg_7d",4)}}, {"$sort":{"date":1}}
        ]))
    return {"department": department, "items": items}


EXPLAIN_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100)):
    if endpoint not in EXPLAIN_ENDPOINTS: fail422("unsupported endpoint")
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: fail422("date_from must be on or before date_to")
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status:
            if status not in STATUSES: fail422("invalid status")
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}
        return mongo_json_safe(client[MONGO_DB].command({"explain": cmd, "verbosity": "executionStats"}))
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
        if not month: fail422("month is required for this endpoint")
        _, _, start_s, end_s = month_filter(month)
        if endpoint == "employee_monthly":
            if not emp_code: fail422("emp_code is required")
            coll, pipeline = "attendance_logs", monthly_employee_pipeline(emp_code, start_s, end_s)
        elif endpoint == "department_summary":
            coll, pipeline = "employees", department_summary_pipeline(start_s, end_s, department)
        else:
            coll, pipeline = "attendance_logs", leaderboard_pipeline(start_s, end_s, limit, department)
        return mongo_json_safe(client[MONGO_DB].command({"explain": {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}))
    if not department or not from_date or not to_date: fail422("department, from and to are required")
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start or (end-start).days+1 > 92: fail422("invalid trend date range")
    pipeline = trend_pipeline(department, start.isoformat(), end.isoformat())
    return mongo_json_safe(client[MONGO_DB].command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}))