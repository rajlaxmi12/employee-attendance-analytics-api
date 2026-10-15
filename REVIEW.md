# REVIEW.md

I reviewed the starter implementation against the API requirements and tested the endpoints with the supplied sample data.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | Employee creation / indexes | Employee codes need database-level uniqueness; application checks alone can race. | Two requests creating the same `emp_code` could otherwise create duplicates. | Added a unique MongoDB index on `emp_code` and return HTTP 409 on `DuplicateKeyError`. |
| 2 | Punch-in | Two simultaneous punch-ins for the same employee and attendance date need atomic duplicate protection. | Concurrent identical requests could both pass a read-before-write check. | Added a unique compound index on `(emp_code, date)` and rely on MongoDB atomic insert uniqueness; the loser receives HTTP 409. |
| 3 | Punch-in / overnight shifts | Calendar date alone is not always the attendance date for a night shift. | A punch after midnight for a 22:00-06:00 shift could be assigned to the wrong day. | Added `attendance_date_for()` to map early-morning punches to the previous attendance date for overnight shifts. |
| 4 | Late calculation | Small delays need the specified grace period. | A punch only a few minutes after shift start could incorrectly count as late. | `compute_late_minutes()` applies the 10-minute grace period before recording late minutes. |
| 5 | Work-hours calculation | Normal floating-point rounding can produce inconsistent boundary results. | Values ending around half-cent precision can round unexpectedly. | Used Decimal `ROUND_HALF_UP` for API calculations and an equivalent MongoDB expression for aggregation results. |
| 6 | Punch-out | Punch-out must be after punch-in and must not create an unrealistic attendance duration. | Invalid timestamps could produce negative or very long work hours. | Validate that punch-out is after punch-in and within 24 hours. |
| 7 | Regularization | Corrections need validation and an audit trail. | Invalid punch ranges/status combinations could be stored, or an existing record could be changed without history. | Validate corrected values, recompute derived fields, and append who changed it, why, when, and the changed values to `history`. |
| 8 | Employee monthly analytics | Monthly statistics should be calculated in MongoDB rather than loading all attendance records into Python. | Larger datasets would require unnecessary application-side scanning and calculation. | Implemented a MongoDB aggregation pipeline for present days, leave days, late counts/minutes, and overtime. |
| 9 | Department summary | Starting the aggregation only from attendance logs would omit employees who have no logs. | Department headcount would be too small when an employee has zero attendance records in the month. | Start from `employees`, `$lookup` attendance logs, preserve empty results, and count distinct employee codes. |
| 10 | Late leaderboard | Ranking and ties need deterministic database-side handling. | Sorting only in Python or assigning sequential positions could handle equal late totals incorrectly. | Aggregate late totals in MongoDB and use `$setWindowFields` with `$rank`, followed by a deterministic final sort. |
| 11 | Department trend | Dates without attendance records need to appear in the trend. | Missing days would disappear and make daily/moving-average results misleading. | Use `$densify`, calculate daily headcount and attendance rate, and `$setWindowFields` for the 7-day moving average. |
| 12 | Admin explain endpoint | Raw MongoDB explain output contains BSON-specific values such as `Timestamp` that FastAPI cannot directly JSON-encode. | `/admin/explain/...` returned HTTP 500 with a `Timestamp object is not iterable` serialization error. | Convert the explain result through `bson.json_util` before returning it, preserving BSON values in JSON-safe Extended JSON form. |

## Reviewed but not considered defects

- Storing attendance `date` and `joined_on` as ISO `YYYY-MM-DD` strings is acceptable here because that format preserves chronological ordering for range comparisons.
- Keeping punch timestamps as MongoDB datetimes internally is appropriate; API serialization converts them to epoch milliseconds where required.
- The department summary intentionally begins with employees rather than attendance logs because employees with no attendance records must still contribute to headcount.
- The unique `(emp_code, date)` attendance index is not only a performance index; it also enforces the one-record-per-employee-per-attendance-date rule under concurrent requests.