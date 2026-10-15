"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
VALID_STATUSES = PRESENCE_STATUSES + ["ABSENT", "LEAVE"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def to_ist(value: datetime) -> datetime:
    # MongoDB returns naive datetime objects in UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def to_mongo(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return truncate_seconds(value.astimezone(UTC)).replace(tzinfo=None)


def time_to_ms(value):
    if value is None:
        return None
    return int(to_ist(value).timestamp() * 1000)


def read_timestamp(value: Optional[int]) -> datetime:
    if value is None:
        return truncate_seconds(datetime.now(IST))
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(422, "punched_at must be an epoch-millisecond integer in the allowed range")
    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(IST))


def parse_date(value: str, field="date") -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must use YYYY-MM-DD")


def month_range(month: str):
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(422, "month must use YYYY-MM")
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "month must use YYYY-MM")
    next_month = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    return first, next_month - timedelta(days=1)


def shift_time(day: date, value: str) -> datetime:
    try:
        hour, minute = map(int, value.split(":"))
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError()
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
    except (ValueError, AttributeError):
        raise HTTPException(422, "Shift times must use HH:MM")


def attendance_date(punch_in: datetime, employee: dict) -> date:
    local = to_ist(punch_in)
    day = local.date()
    start = tuple(map(int, employee["shift_start"].split(":")))
    end = tuple(map(int, employee["shift_end"].split(":")))
    # An overnight shift's after-midnight punch belongs to the previous day.
    if end <= start and (local.hour, local.minute) < end:
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = to_ist(punch_in)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    seconds_late = (local - start).total_seconds()
    # Exactly 10 minutes is not late. Once over 10 minutes, floor whole minutes.
    return int(seconds_late // 60) if seconds_late > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (to_ist(punch_out) - to_ist(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str,
                     shift_start: str = "09:30") -> int:
    day = parse_date(date_str)
    start = tuple(map(int, shift_start.split(":")))
    end_h, end_m = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if (end_h, end_m) <= start else day
    end = shift_time(end_day, shift_end)
    seconds = (to_ist(punch_out) - end).total_seconds()
    minutes = int(seconds // 60) if seconds > 0 else 0
    return minutes if minutes >= 30 else 0


def require_employee(emp_code: str):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
    return employee


def check_status(status: str):
    if status not in VALID_STATUSES:
        raise HTTPException(422, "Invalid attendance status")


def check_page(page: int, page_size: int):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "page must be at least 1 and page_size must be 1 to 100")



def public_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("id", None)
    doc["punch_in"] = time_to_ms(doc.get("punch_in"))
    doc["punch_out"] = time_to_ms(doc.get("punch_out"))
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)

    # Return history in the exact API contract format.
    converted_history = []
    for entry in doc["history"]:
        item = dict(entry)

        # Support old history entries without exposing their incorrect shape.
        at = item.get("at", item.get("changed_at"))
        item["at"] = time_to_ms(at) if isinstance(at, datetime) else at
        item["by"] = item.get("by", item.get("regularized_by", ""))
        item.setdefault("reason", "")
        item.setdefault("changes", {})

        for key in ("changed_at", "regularized_by", "changed_fields"):
            item.pop(key, None)

        converted_history.append(item)

    doc["history"] = converted_history
    return doc




def create_indexes():
    # Idempotent indexes; the unique indexes also protect against concurrent requests.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)],
                                    name="ix_attendance_status_date")


@app.on_event("startup")
def startup():
    create_indexes()


def explain_command(command: dict):
    return db.command("explain", command, verbosity="executionStats")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=1)
    department: str = Field(min_length=1)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        parse_date(value, "joined_on")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("shift time must use HH:MM")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class AttendancePatch(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=1)
    regularized_by: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Endpoints provided (corrected)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok", "database": "ok"}
    except PyMongoError:
        raise HTTPException(503, "database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    check_page(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0})
                 .sort("emp_code", ASCENDING)
                 .skip((page - 1) * page_size).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = require_employee(body.emp_code)
    check_status(body.status)
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, "Punch-in status must be PRESENT, WFH, or ON_DUTY")
    timestamp = read_timestamp(body.punched_at)
    day = attendance_date(timestamp, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": to_mongo(timestamp),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(timestamp, employee["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None,
                    date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    status: Optional[str] = None,
                    page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100)):
    check_page(page, page_size)
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        check_status(status)
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_attendance(doc) for doc in docs],
            "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Missing endpoints required by the contract
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = require_employee(body.emp_code)
    timestamp = read_timestamp(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": to_mongo(timestamp)}, "punch_out": None},
        sort=[("punch_in", DESCENDING)]
    )
    if not record:
        raise HTTPException(404, "No open punch-in record found")
    punch_in_time = to_ist(record["punch_in"])
    if timestamp <= punch_in_time or timestamp > punch_in_time + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(punch_in_time, timestamp)
    update = {
        "punch_out": to_mongo(timestamp),
        "work_hours": hours,
        "overtime_minutes": compute_overtime(timestamp, employee["shift_end"],
                                              record["date"], employee["shift_start"]),
        "half_day": hours < 4.50,
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": update}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance was already punched out")
    record.update(update)
    return public_attendance(record)


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: AttendancePatch):
    employee = require_employee(emp_code)
    parse_date(date)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")
    supplied = body.model_fields_set
    if not ({"status", "punch_in", "punch_out"} & supplied):
        raise HTTPException(422, "Provide a field to change")
    status = body.status if "status" in supplied else record["status"]
    check_status(status)
    if status in ("ABSENT", "LEAVE") and any(
        key in supplied and getattr(body, key) is not None for key in ("punch_in", "punch_out")
    ):
        raise HTTPException(422, "ABSENT/LEAVE cannot have supplied punch times")

    punch_in_time = (read_timestamp(body.punch_in) if "punch_in" in supplied and body.punch_in is not None
                     else to_ist(record["punch_in"]) if record.get("punch_in") else None)
    punch_out_time = (read_timestamp(body.punch_out) if "punch_out" in supplied and body.punch_out is not None
                      else to_ist(record["punch_out"]) if record.get("punch_out") else None)
    if status in ("ABSENT", "LEAVE"):
        punch_in_time = punch_out_time = None
    elif punch_in_time is None:
        raise HTTPException(422, "Presence status requires punch_in")
    if punch_out_time is not None and (
        punch_in_time is None or punch_out_time <= punch_in_time
        or punch_out_time > punch_in_time + timedelta(hours=24)
    ):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    if punch_in_time and attendance_date(punch_in_time, employee).isoformat() != date:
        raise HTTPException(422, "punch_in must belong to this attendance date")

    changes = {"status": status}
    if punch_in_time is None:
        changes.update({"punch_in": None, "late_minutes": 0})
    else:
        changes.update({"punch_in": to_mongo(punch_in_time),
                        "late_minutes": compute_late_minutes(punch_in_time, employee["shift_start"])})
    if punch_out_time is None:
        changes.update({"punch_out": None, "work_hours": None, "overtime_minutes": 0, "half_day": False})
    else:
        hours = compute_work_hours(punch_in_time, punch_out_time)
        changes.update({
            "punch_out": to_mongo(punch_out_time), "work_hours": hours,
            "overtime_minutes": compute_overtime(punch_out_time, employee["shift_end"], date, employee["shift_start"]),
            "half_day": hours < 4.50,
        })
    changed = {key: value for key, value in changes.items() if record.get(key) != value}
    if not changed:
        raise HTTPException(422, "No changes to apply")

    history = record.get("history", [])

    history_changes = {}
    for key, new_value in changed.items():
        old_value = record.get(key)

        if isinstance(old_value, datetime):
            old_value = time_to_ms(old_value)
        if isinstance(new_value, datetime):
            new_value = time_to_ms(new_value)

        history_changes[key] = {
            "from": old_value,
            "to": new_value,
        }

    history_entry = {
        "at": int(datetime.now(UTC).timestamp() * 1000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes,
    }

    # Compare-and-set prevents concurrent updates from overwriting history.
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "history": history},
        {"$set": changed, "$push": {"history": history_entry}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Attendance changed concurrently; reload and retry")
    record.update(changed)
    record["history"] = history + [history_entry]
    return public_attendance(record)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    employee = require_employee(emp_code)
    first, last = month_range(month)
    joined = max(first, parse_date(employee["joined_on"], "joined_on"))
    working_days = sum(
        1 for offset in range((last - joined).days + 1)
        if (joined + timedelta(days=offset)).weekday() < 5
    ) if joined <= last else 0
    # Aggregation keeps attendance calculations in MongoDB.
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENCE_STATUSES]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}
    present = stats.get("present_days", 0)
    return {
        "emp_code": emp_code, "name": employee["name"], "department": employee["department"],
        "month": month, "working_days": working_days,
        "present_days": round_half_up(present, 2), "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(present / working_days * 100, 4) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = month_range(month)
    employee_query = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_query["department"] = department
    employees = list(db.employees.find(employee_query, {"_id": 0, "emp_code": 1, "department": 1}))
    if not employees:
        return {"month": month, "items": []}
    employee_codes = [employee["emp_code"] for employee in employees]
    stats_pipeline = [
        {"$match": {"emp_code": {"$in": employee_codes},
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENCE_STATUSES]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0
            ]}},
            "hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, {"$ne": ["$work_hours", None]}]},
                "$work_hours", 0
            ]}},
            "hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE_STATUSES]}, {"$ne": ["$work_hours", None]}]}, 1, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}
        }}
    ]
    stats = {item["_id"]: item for item in db.attendance_logs.aggregate(stats_pipeline)}
    grouped = {}
    for employee in employees:
        dept = employee["department"]
        row = grouped.setdefault(dept, {
            "department": dept, "headcount": 0, "present_days": 0,
            "_hours_sum": 0, "_hours_count": 0, "late_count": 0,
            "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0
        })
        row["headcount"] += 1
        values = stats.get(employee["emp_code"], {})
        for key in ("present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            row[key] += values.get(key, 0)
        row["_hours_sum"] += values.get("hours_sum", 0)
        row["_hours_count"] += values.get("hours_count", 0)
    items = []
    for dept in sorted(grouped):
        row = grouped[dept]
        row["present_days"] = round_half_up(row["present_days"], 2)
        row["avg_work_hours"] = round_half_up(row["_hours_sum"] / row["_hours_count"], 2) if row["_hours_count"] else None
        del row["_hours_sum"], row["_hours_count"]
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=100),
                     department: Optional[str] = None):
    first, last = month_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "rank": 1}},
        {"$sort": {"rank": 1, "emp_code": 1}}
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    # Densify dates in MongoDB so days with no attendance records are included.
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$match": {"$or": [
            {"logs": None},
            {"logs.date": {"$gte": from_date, "$lte": to_date}}
        ]}},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$logs.status", PRESENCE_STATUSES]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$match": {"_id": {"$ne": None}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$sort": {"day": 1}},
        {"$densify": {"field": "day", "range": {
            "step": 1, "unit": "day",
            "bounds": [datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())]
        }}},
        {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                      "present_count": 1, "late_count": 1}}
    ]
    data = list(db.employees.aggregate(pipeline))
    # Headcount depends on each day's joined_on value.
    items = []
    for item in data:
        day_string = item["date"]
        day_value = parse_date(day_string)
        headcount = db.employees.count_documents({
            "department": department, "joined_on": {"$lte": day_string}
        })
        is_working_day = day_value.weekday() < 5
        present = round_half_up(item.get("present_count", 0), 2)
        rate = round_half_up(present / headcount, 4) if is_working_day and headcount else (0.0 if is_working_day else None)
        items.append({
            "date": day_string, "is_working_day": is_working_day, "headcount": headcount,
            "present_count": present, "late_count": item.get("late_count", 0),
            "attendance_rate": rate
        })
    for index, item in enumerate(items):
        rates = [row["attendance_rate"] for row in items[max(0, index - 6):index + 1]
                 if row["attendance_rate"] is not None]
        item["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4) if rates else None
    return {"department": department, "from": from_date, "to": to_date, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal[
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend"
], month: Optional[str] = None, emp_code: Optional[str] = None,
   department: Optional[str] = None, date_from: Optional[str] = None,
   date_to: Optional[str] = None, page: int = 1, page_size: int = 20,
   limit: int = 10):
    check_page(page, page_size)
    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        command = {"find": "attendance_logs", "filter": query,
                   "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size}
        return explain_command(command)

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        require_employee(emp_code)
        first, last = month_range(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
        return explain_command({"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_range(month)
        pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
        return explain_command({"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last = month_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit}
        ]
        return explain_command({"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})

    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(422, "department, from, and to are required")
        start, end = parse_date(date_from, "from"), parse_date(date_to, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "Invalid date range")
        pipeline = [
            {"$match": {"department": department}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code",
                         "foreignField": "emp_code", "as": "logs"}}
        ]
        return explain_command({"aggregate": "employees", "pipeline": pipeline, "cursor": {}})
