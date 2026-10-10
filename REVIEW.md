# Review of the starter app/main.py

| # | Where | What is wrong | How you notice it | Fix |
|---|---|---|---|---|
| 1 | startup | No indexes, no unique constraint | Duplicates under load; COLLSCAN in explain | Unique indexes on emp_code and (emp_code, date), plus query indexes, created at startup |
| 2 | /health | Always returns ok | Stop MongoDB, still 200 | Ping MongoDB, return 503 on failure |
| 3 | POST /employees | Check-then-insert race; no validation; datetime.now() is local naive time; created_at not epoch ms | Two parallel POSTs both get 201; joined_on "abc" accepted | Pydantic validation, DuplicateKeyError gives 409, UTC time, epoch ms output |
| 4 | GET /employees | skip = page*page_size skips page 1; total ignores the filter; no sort; page_size unbounded | page=1 returns the second page; department filter shows the wrong total | (page-1)*page_size, count with the filter, sort by emp_code, page_size max 100 |
| 5 | POST /punch-in | Unknown employee gives a 500; local-time conversion; date not IST and ignores overnight shifts (R1); no truncation to seconds; any status accepted; check-then-insert race; response leaks "id" and raw datetimes; seconds-looking timestamps accepted | Unknown emp_code gives 500; punched_at=1784520900 is accepted | 404 check, IST date logic, seconds truncation, allowed statuses only, epoch-ms validation, insert guarded by unique index |
| 6 | compute_late_minutes | Floors minutes then tests > 10, so 09:40:01 gives 0 instead of 10; uses the punch-in's own date and zone; breaks for overnight shifts | 09:40:01 on a 09:30 shift | Compare in seconds (> 600), then floor; shift start taken from the attendance date in IST |
| 7 | compute_work_hours | round() is float/banker's rounding, not half-up (R4) | Exact .xx5 values round the wrong way | Decimal with ROUND_HALF_UP |
| 8 | compute_overtime | No 30-minute minimum (R3); same-day shift end breaks overnight shifts; replace(tzinfo) on a naive date | 5 minutes after shift end counted as 5 | Shift end computed per attendance date (+1 day if overnight); 0 unless >= 30 |
| 9 | GET /attendance | Loads the whole collection and sorts in Python; sorts by date only; no date_from > date_to check; unvalidated status; leaks id; missing history/half_day not defaulted | Slow at 100k records; COLLSCAN | Query with sort (date desc, emp_code asc) + skip/limit using a matching index; validation; defaults |
| 10 | whole file | PyMongo returns naive datetimes that are later treated as local time | Times off by 5.5 hours | tz_aware=True, convert to epoch ms at the boundary |

Examined and fine: endpoints are sync `def`, so FastAPI runs them in a
threadpool and blocking PyMongo calls do not block the event loop.
load_dotenv() does not override real environment variables.