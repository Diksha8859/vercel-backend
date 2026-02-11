from typing import Optional, List, Dict, Any

# pyright: reportMissingImports=false

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from datetime import date, datetime
import uuid
import os

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

app = FastAPI(title="HRMS Lite API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── MongoDB store ───────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set. Define it in .env or environment.")
client = MongoClient(MONGO_URI)
db = client["hrms_lite"]
employees_coll = db["employees"]
attendance_coll = db["attendance"]

# Ensure useful indexes
employees_coll.create_index("employee_id", unique=True)
employees_coll.create_index("email", unique=True)
attendance_coll.create_index(
    [("employee_id", 1), ("date", 1)],
    unique=True,
    name="employee_date_unique",
)

# ── Models ─────────────────────────────────────────────────────────────────────
DEPARTMENTS = ["Engineering", "Marketing", "Sales", "HR", "Finance", "Operations", "Design", "Product"]

class EmployeeCreate(BaseModel):
    employee_id: str
    full_name: str
    email: str
    department: str

    @field_validator("employee_id")
    @classmethod
    def validate_employee_id(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Employee ID cannot be empty")
        return v

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Full name must be at least 2 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Invalid email address")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if v not in DEPARTMENTS:
            raise ValueError(f"Department must be one of: {', '.join(DEPARTMENTS)}")
        return v

class AttendanceCreate(BaseModel):
    employee_id: str
    date: str   # ISO format: YYYY-MM-DD
    status: str  # "Present" | "Absent"

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in ("Present", "Absent"):
            raise ValueError("Status must be 'Present' or 'Absent'")
        return v

    @field_validator("date")
    @classmethod
    def validate_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v

# ── Employee Endpoints ─────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"message": "HRMS Lite API is running", "version": "1.0.0"}

@app.get("/employees", response_model=List[dict])
def get_employees():
    # Fetch all employees from MongoDB (exclude _id)
    emps: List[Dict[str, Any]] = list(
        employees_coll.find({}, {"_id": 0})
    )
    # Attach total_present count to each employee
    for emp in emps:
        eid = emp["employee_id"]
        emp["total_present"] = attendance_coll.count_documents(
            {"employee_id": eid, "status": "Present"}
        )
    return emps

@app.post("/employees", status_code=201)
def create_employee(employee: EmployeeCreate):
    record = employee.model_dump()
    record["created_at"] = datetime.utcnow().isoformat()

    try:
        employees_coll.insert_one(record)
    except DuplicateKeyError as e:
        msg = str(e)
        if "employee_id" in msg:
            raise HTTPException(
                status_code=409,
                detail=f"Employee ID '{employee.employee_id}' already exists",
            )
        if "email" in msg:
            raise HTTPException(
                status_code=409,
                detail=f"Email '{employee.email}' already registered",
            )
        raise HTTPException(status_code=409, detail="Duplicate employee")

    record.pop("_id", None)
    return {"message": "Employee created successfully", "employee": record}

@app.get("/employees/{employee_id}")
def get_employee(employee_id: str):
    emp = employees_coll.find_one({"employee_id": employee_id}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    emp["total_present"] = attendance_coll.count_documents(
        {"employee_id": employee_id, "status": "Present"}
    )
    return emp

@app.delete("/employees/{employee_id}")
def delete_employee(employee_id: str):
    result = employees_coll.delete_one({"employee_id": employee_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")

    attendance_coll.delete_many({"employee_id": employee_id})
    return {"message": "Employee and related attendance records deleted successfully"}

# ── Attendance Endpoints ───────────────────────────────────────────────────────
@app.get("/attendance")
def get_all_attendance(
    employee_id: Optional[str] = Query(None),
    date_filter: Optional[str] = Query(None, alias="date")
):
    query: Dict[str, Any] = {}
    if employee_id:
        query["employee_id"] = employee_id
    if date_filter:
        query["date"] = date_filter

    records: List[Dict[str, Any]] = list(
        attendance_coll.find(query, {"_id": 0}).sort("date", -1)
    )
    return records

@app.post("/attendance", status_code=201)
def mark_attendance(attendance: AttendanceCreate):
    # Ensure employee exists
    if not employees_coll.find_one({"employee_id": attendance.employee_id}):
        raise HTTPException(status_code=404, detail="Employee not found")

    record = attendance.model_dump()
    record["id"] = str(uuid.uuid4())
    record["marked_at"] = datetime.utcnow().isoformat()

    # Upsert by employee_id + date
    result = attendance_coll.update_one(
        {"employee_id": attendance.employee_id, "date": attendance.date},
        {"$set": record},
        upsert=True,
    )

    action = "updated" if result.matched_count > 0 else "created"
    record.pop("_id", None)
    return {"message": f"Attendance {action} successfully", "record": record}

@app.delete("/attendance/{employee_id}/{date}")
def delete_attendance(employee_id: str, date: str):
    result = attendance_coll.delete_one({"employee_id": employee_id, "date": date})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    return {"message": "Attendance record deleted"}

# ── Dashboard / Summary ────────────────────────────────────────────────────────
@app.get("/dashboard/summary")
def get_dashboard_summary():
    total_employees = employees_coll.count_documents({})
    today = date.today().isoformat()
    present_today = attendance_coll.count_documents(
        {"date": today, "status": "Present"}
    )
    absent_today = attendance_coll.count_documents(
        {"date": today, "status": "Absent"}
    )
    dept_distribution: Dict[str, int] = {}
    for emp in employees_coll.find({}, {"_id": 0, "department": 1}):
        dept = emp["department"]
        dept_distribution[dept] = dept_distribution.get(dept, 0) + 1

    return {
        "total_employees": total_employees,
        "present_today": present_today,
        "absent_today": absent_today,
        "not_marked_today": total_employees - present_today - absent_today,
        "department_distribution": dept_distribution,
        "today": today,
        "departments": DEPARTMENTS,
    }
