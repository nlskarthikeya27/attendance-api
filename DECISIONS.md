# Decisions

**Indexes.** employees: unique emp_code; (department, emp_code) for the
filtered, sorted list; (joined_on, department) for headcount. attendance_logs:
unique (emp_code, date) which enforces one record per day and serves monthly
and trend queries; (date desc, emp_code) for the list sort and for date-range
analytics; (emp_code, punch_in desc) to find the record to punch out.

**Two identical punch-ins at the same instant.** Both try insert_one. The
unique (emp_code, date) index lets exactly one succeed; the other gets
DuplicateKeyError and returns 409. There is no check-then-insert.

**Leaderboard tie at the cutoff.** $setWindowFields with $rank gives
competition ranking (1, 2, 2, 4). I filter rank <= limit after ranking, so all
tied employees at the cutoff are returned, which can be more than `limit` rows.

**Employees with no logs.** The department summary starts from `employees`,
filters joined_on <= month end, $lookups the logs and unwinds with
preserveNullAndEmptyArrays. Headcount is the set of distinct emp_codes, so
people with no logs count and people who join later do not.

**100x the data.** Pre-aggregate monthly numbers into a summary collection,
use cursor-based pagination instead of skip, store department on each log to
avoid $lookup and big $in lists, and shard attendance_logs by emp_code.