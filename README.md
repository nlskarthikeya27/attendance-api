# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
    python -m venv venv
    venv\Scripts\activate          (Linux/Mac: source venv/bin/activate)
    pip install -r requirements.txt
    set MONGO_URI=mongodb://localhost:27017
    set MONGO_DB=attendance_db
    uvicorn app.main:app --port 8000

A local `.env` with MONGO_URI and MONGO_DB also works; real environment
variables take priority. Indexes are created automatically at startup.
Interactive docs: http://localhost:8000/docs

## Status
All endpoints in openapi.yaml are implemented, including /admin/explain.
Tested by hand against the sample data (punch-in/out, 409/404/422 cases,
PATCH with history, all analytics endpoints, explain shows IXSCAN).
Not tested: the 100k-record dataset and truly parallel requests.