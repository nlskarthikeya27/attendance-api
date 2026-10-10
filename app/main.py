"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional
import calendar

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
ATT_SORT = [("date", -1), ("emp_code", 1)]


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time conversion
# --------------------------------------------------------------------------- #
def now_sec() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_date(s: str) -> Date:
    if not DATE_RE.fullmatch(s):
        raise HTTPException(422, f"invalid date: {s}")
    try:
        return Date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"invalid date: {s}")


def month_range(month: str):
    if not re.fullmatch(MONTH_RE, month):
        raise HTTPException(422, f"invalid month: {month}")
    y, m = int(month[:4]), int(month[5:])
    first = Date(y, m, 1)
    last = Date(y, m, calendar.monthrange(y, m)[1])
    return first, last


def r_half_up(x, places: int):
    if x is None:
        return None
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(round(float(x), 10))).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Helpers: business rules R1 - R5
# --------------------------------------------------------------------------- #
def minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(emp) -> bool:
    return minutes_of(emp["shift_end"]) <= minutes_of(emp["shift_start"])


def attendance_date(punch_in: datetime, emp) -> Date:
    """R1: IST date of punch-in; overnight early-morning punch-ins belong to the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(emp):
        secs = ist.hour * 3600 + ist.minute * 60 + ist.second
        if secs < minutes_of(emp["shift_end"]) * 60:
            d -= timedelta(days=1)
    return d


def shift_bounds(d: Date, emp):
    sh, sm = divmod(minutes_of(emp["shift_start"]), 60)
    eh, em = divmod(minutes_of(emp["shift_end"]), 60)
    start = datetime.combine(d, dtime(sh, sm), tzinfo=IST)
    end = datetime.combine(d, dtime(eh, em), tzinfo=IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    delta = (punch_in - shift_start).total_seconds()
    return int(delta // 60) if delta > 600 else 0  # strictly more than 10:00 (R2)


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    mins = int((punch_out - shift_end).total_seconds() // 60)
    return mins if mins >= 30 else 0  # R3


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(emp, att_date: Date, status: str, pin, pout) -> dict:
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or pin is None:
        return out
    start, end = shift_bounds(att_date, emp)
    out["late_minutes"] = compute_late_minutes(pin, start)
    if pout is not None:
        wh = compute_work_hours(pin, pout)
        out["work_hours"] = wh
        out["overtime_minutes"] = compute_overtime(pout, end)
        out["half_day"] = wh < 4.5  # R5, on the rounded value
    return out


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API JSON)
# --------------------------------------------------------------------------- #
def _conv(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def ser_history(h) -> dict:
    changes = {
        k: {"from": _conv(v.get("from")), "to": _conv(v.get("to"))}
        for k, v in (h.get("changes") or {}).items()
    }
    return {"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def ser_att(d) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [ser_history(h) for h in (d.get("history") or [])],
    }


def ser_emp(d) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d["name"], "email": d["email"],
        "department": d["department"], "shift_start": d["shift_start"],
        "shift_end": d["shift_end"], "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _check_date(cls, v):
        if not DATE_RE.fullmatch(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
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


class RegularizeIn(BaseModel):  # unknown fields (e.g. derived ones) are ignored
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_sec()
    try:
        db.employees.insert_one(doc)  # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_emp(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    skip = min((page - 1) * page_size, 2**62)
    total = db.employees.count_documents(q)
    items = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    return {"items": [ser_emp(d) for d in items], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_sec()
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, d, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_att(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_sec()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    d = Date.fromisoformat(rec["date"])
    derived = derive(emp, d, rec["status"], pin, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # only succeeds for the first caller
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return ser_att(updated)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    f = parse_date(date_from) if date_from else None
    t = parse_date(date_to) if date_to else None
    if f and t and f > t:
        raise HTTPException(422, "date_from must not be after date_to")
    if f or t:
        q["date"] = {}
        if f:
            q["date"]["$gte"] = f.isoformat()
        if t:
            q["date"]["$lte"] = t.isoformat()
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    skip = min((page - 1) * page_size, 2**62)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip(skip).limit(page_size)
    return {"items": [ser_att(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    old_status = rec["status"]
    new_status = body.status or old_status
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_pin = new_pout = None
    else:
        new_pin = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_pout = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_pin is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_pin, emp).isoformat() != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_pout is not None and (new_pout <= new_pin or new_pout - new_pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = derive(emp, Date.fromisoformat(date), new_status, new_pin, new_pout)
    new = {"status": new_status, "punch_in": new_pin, "punch_out": new_pout, **derived}
    old = {
        "status": old_status,
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_sec(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: only apply if the record is still exactly as we read it.
    flt = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(
        flt, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return ser_att(updated)


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders
# --------------------------------------------------------------------------- #
def _dow(p):
    return {"$dayOfWeek": {"$dateFromString": {
        "dateString": f"{p}date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}


def present_val(p: str, weekdays_only: bool = True):
    """1 for a present record, 0.5 for a half day, 0 otherwise (R5-R7). p = field prefix."""
    cond = {"$in": [f"{p}status", PRESENCE]}
    if weekdays_only:
        cond = {"$and": [cond, {"$in": [_dow(p), [2, 3, 4, 5, 6]]}]}
    return {"$cond": [cond, {"$cond": [{"$eq": [{"$ifNull": [f"{p}half_day", False]}, True]}, 0.5, 1]}, 0]}


def _late_flag(p):
    return {"$cond": [{"$gt": [{"$ifNull": [f"{p}late_minutes", 0]}, 0]}, 1, 0]}


def _status_flag(p, s):
    return {"$cond": [{"$eq": [f"{p}status", s]}, 1, 0]}


def _wh_ok(p):
    return {"$and": [
        {"$in": [f"{p}status", PRESENCE]},
        {"$in": [{"$type": f"{p}work_hours"}, ["double", "int", "long", "decimal"]]},
    ]}


def working_days(first: Date, last: Date, joined_on: Date) -> int:
    start = max(first, joined_on)
    if start > last:
        return 0
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def employee_monthly_pipeline(emp_code, first: Date, last: Date):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_val("$")},
            "leave_days": {"$sum": _status_flag("$", "LEAVE")},
            "late_count": {"$sum": _late_flag("$")},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: Date, last: Date, department):
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    p = "$logs."
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": present_val(p)},
            "wh_sum": {"$sum": {"$cond": [_wh_ok(p), f"{p}work_hours", 0]}},
            "wh_cnt": {"$sum": {"$cond": [_wh_ok(p), 1, 0]}},
            "late_count": {"$sum": _late_flag(p)},
            "total_late_minutes": {"$sum": {"$ifNull": [f"{p}late_minutes", 0]}},
            "leave_count": {"$sum": _status_flag(p, "LEAVE")},
            "on_duty_count": {"$sum": _status_flag(p, "ON_DUTY")},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": {"$size": "$emps"},
            "present_days": 1, "wh_sum": 1, "wh_cnt": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(first: Date, last: Date, limit: int, department):
    pipe = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})
    pipe += [
        # $rank = standard competition ranking (1, 2, 2, 4); sort ONLY by the metric so ties share a rank
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_inputs(department: str):
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


def check_trend_range(frm: str, to: str):
    f, t = parse_date(frm), parse_date(to)
    if t < f:
        raise HTTPException(422, "to must not be before from")
    if (t - f).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return f, t


def trend_pipeline(codes, joined, f: Date, t: Date):
    lo = datetime.combine(f, dtime.min, tzinfo=UTC)
    hi = datetime.combine(t + timedelta(days=1), dtime.min, tzinfo=UTC)  # upper bound is exclusive
    p2 = {"$multiply": ["$present_count", 2]}
    # half-up to 4 decimals using integer-style arithmetic: floor((2*p*10000 + h) / (2h)) / 10000
    rate = {"$divide": [{"$floor": {"$divide": [
        {"$add": [{"$multiply": [p2, 10000]}, "$headcount"]}, {"$multiply": ["$headcount", 2]}]}}, 10000]}
    return [
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": f.isoformat(), "$lte": t.isoformat()}}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": present_val("$", weekdays_only=False)},
                    "late_count": {"$sum": _late_flag("$")}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {"headcount": {"$size": {"$filter": {
            "input": joined, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, rate, None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    res = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    g = res[0] if res else {}
    present = g.get("present_days", 0)
    wd = working_days(first, last, Date.fromisoformat(emp["joined_on"]))
    pct = None
    if wd > 0:
        pct = float((Decimal(str(round(float(present), 10))) * 100 / wd)
                    .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": r_half_up(present, 2), "leave_days": g.get("leave_days", 0),
        "late_count": g.get("late_count", 0), "total_late_minutes": g.get("total_late_minutes", 0),
        "total_overtime_minutes": g.get("total_overtime_minutes", 0), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_range(month)
    rows = db.employees.aggregate(department_summary_pipeline(first, last, department))
    items = []
    for r in rows:
        avg = None
        if r["wh_cnt"] > 0:
            avg = float((Decimal(str(round(float(r["wh_sum"]), 10))) / r["wh_cnt"])
                        .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["department"], "headcount": r["headcount"],
            "present_days": r_half_up(r["present_days"], 2), "avg_work_hours": avg,
            "late_count": r["late_count"], "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"], "on_duty_count": r["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_range(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: str = Query(..., alias="from"), to: str = Query(...)):
    f, t = check_trend_range(frm, to)
    codes, joined = trend_inputs(department)
    rows = db.attendance_logs.aggregate(trend_pipeline(codes, joined, f, t))
    items = [{
        "date": r["date"], "is_working_day": r["is_working_day"], "headcount": int(r["headcount"]),
        "present_count": r_half_up(r["present_count"], 2), "late_count": int(r["late_count"]),
        "attendance_rate": r_half_up(r["attendance_rate"], 4),
        "moving_avg_7d": r_half_up(r["moving_avg_7d"], 4),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _need(**kw):
    missing = [k for k, v in kw.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


def _explain(body: dict) -> dict:
    res = db.command({"explain": body, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(res))


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        body = {"find": "attendance_logs", "filter": q, "sort": dict(ATT_SORT),
                "skip": min((page - 1) * page_size, 2**62), "limit": page_size}
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        first, last = month_range(month)
        body = {"aggregate": "attendance_logs",
                "pipeline": employee_monthly_pipeline(emp_code, first, last), "cursor": {}}
        coll = "attendance_logs"
    elif endpoint == "department_summary":
        _need(month=month)
        first, last = month_range(month)
        body = {"aggregate": "employees",
                "pipeline": department_summary_pipeline(first, last, department), "cursor": {}}
        coll = "employees"
    elif endpoint == "late_leaderboard":
        _need(month=month)
        first, last = month_range(month)
        body = {"aggregate": "attendance_logs",
                "pipeline": leaderboard_pipeline(first, last, limit, department), "cursor": {}}
        coll = "attendance_logs"
    else:  # department_trend
        _need(department=department, **{"from": frm, "to": to})
        f, t = check_trend_range(frm, to)
        codes, joined = trend_inputs(department)
        body = {"aggregate": "attendance_logs",
                "pipeline": trend_pipeline(codes, joined, f, t), "cursor": {}}
        coll = "attendance_logs"
    return {"endpoint": endpoint, "collection": coll, "explain": _explain(body)}