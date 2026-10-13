# ShiftLine API: Employee Attendance & Analytics

FastAPI + MongoDB service for punch-in/out, attendance corrections and attendance analytics.
All application code is in `app/main.py`.

## Requirements
- Python 3.10+
- MongoDB 6.0 or newer (tested on 7.0)

## Run locally
```bash
# 1. start MongoDB (any one way)
docker run -d -p 27017:27017 mongo:7

# 2. install
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. configure (optional, defaults: mongodb://localhost:27017 and attendance_db)
cp .env.example .env               # real environment variables override .env

# 4. load sample data (optional)
python sample_seed.py

# 5. start the API
uvicorn app.main:app --port 8000
```
Interactive docs: http://localhost:8000/docs

## Configuration
| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

Indexes are created automatically at startup (safe to run repeatedly).

## Endpoints
| Method | Path |
|---|---|
| GET | `/health` |
| POST, GET | `/employees` |
| POST | `/attendance/punch-in`, `/attendance/punch-out` |
| GET | `/attendance` |
| PATCH | `/attendance/{emp_code}/{date}` |
| GET | `/analytics/employees/{emp_code}/monthly` |
| GET | `/analytics/departments/summary` |
| GET | `/analytics/leaderboard/late` |
| GET | `/analytics/departments/{department}/trend` |
| GET | `/admin/explain/{endpoint}` |

## Notes
- Times in requests and responses are epoch milliseconds; they are stored in UTC and truncated to seconds. Business rules use IST.
- Defects found in the starter are listed in `REVIEW.md`; design choices are in `DECISIONS.md`.
