# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, regularization, and attendance analytics.

## Tech Stack

- Python 3.12
- FastAPI
- MongoDB / PyMongo
- Pydantic
- Uvicorn

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
pip install -r requirements.txt
```

Create `.env` from `.env.example` and configure your MongoDB connection:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Seed the supplied sample data:

```bash
python sample_seed.py
```

Run the API:

```bash
uvicorn app.main:app --port 8000
```

Swagger documentation is available at:

```text
http://localhost:8000/docs
```

## Features

- Employee creation and listing
- Punch-in and punch-out attendance
- Overnight shift handling
- Attendance regularization with correction history
- Monthly employee attendance analytics
- Department summary analytics
- Late-arrival leaderboard with tie-aware ranking
- Department attendance trend and 7-day moving average
- MongoDB query-plan inspection through the admin explain endpoint
- Database-level uniqueness protection for employees and attendance records

## MongoDB

The application uses MongoDB aggregation pipelines for analytics rather than loading the complete dataset into application memory.

Important indexes include:

- Unique employee code
- Unique `(emp_code, date)` attendance record
- Additional indexes supporting attendance and analytics queries

MongoDB 6.0 or newer is required.

## API Testing

The supplied sample dataset can be loaded with `sample_seed.py`. The API can then be tested through Swagger UI or any HTTP client.

The implementation handles validation errors, duplicate/conflicting operations, attendance calculations, pagination, and analytics according to the supplied API contract.

## Project Structure

```text
app/
  main.py
  __init__.py
sample_data/
sample_seed.py
requirements.txt
README.md
REVIEW.md
DECISIONS.md
DATA_MODEL.md
openapi.yaml
.env.example
```

## Notes

`REVIEW.md` documents defects identified and addressed during implementation.

`DECISIONS.md` explains the main design decisions, including indexes, concurrent punch-ins, leaderboard ties, department headcount, and scaling considerations.

Secrets are read from environment variables. The local `.env` file is intentionally excluded from version control.