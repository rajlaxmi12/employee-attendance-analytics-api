# REVIEW.md

I reviewed the starter `app/main.py` against the supplied API contract and business rules. These are the defects I found and fixed.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health` | The starter health endpoint does not actually verify MongoDB connectivity. | The API can report healthy even when MongoDB is unavailable. | Added a MongoDB `ping` and return the appropriate service status when the database cannot be reached. |
| 2 | Employee listing | Pagination uses the wrong skip calculation. | Page 1 skips the first page of employees when `page * page_size` is used. | Changed it to `(page - 1) * page_size`. |
| 3 | Employee listing | `total` can be calculated without applying the same department filter used for the result set. | A filtered response can report a total larger than the number of matching employees. | Use the same MongoDB filter for both `find()` and `count_documents()`. |
| 4 | Employee listing | Results are not guaranteed to have deterministic ordering. | Records can move between pages depending on MongoDB's natural order. | Added an explicit sort before skip/limit pagination. |
| 5 | API responses | MongoDB's internal `_id` can leak into API responses. | Responses expose a database implementation detail not present in the API contract. | Remove `_id` from projections/responses and return only contract fields. |
| 6 | Punch-in | The starter does not safely handle an unknown employee. | Punch-in for an invalid `emp_code` can fail incorrectly instead of returning the required not-found response. | Explicitly validate that the employee exists and return HTTP 404 when it does not. |
| 7 | Punch-in | Duplicate prevention based only on checking first and inserting later is race-prone. | Two simultaneous punch-ins can both pass the initial check. | Added a unique compound MongoDB index on `(emp_code, date)` and handle `DuplicateKeyError` as HTTP 409. |
| 8 | Punch-in / attendance date | Attendance date handling does not correctly account for IST and overnight shifts. | A punch after midnight for a 22:00-06:00 shift can be assigned to the next calendar date instead of the intended attendance date. | Normalize timestamps and calculate the attendance date using the employee's shift, including overnight shifts. |
| 9 | Late calculation | The late-minute boundary/grace calculation is incorrect for partial minutes. | A timestamp such as 09:40:01 can produce the wrong late-minute result. | Calculate lateness after the grace period using the required whole-minute business rule. |
| 10 | Overtime calculation | The starter overtime calculation does not fully apply the overtime threshold and overnight-shift rules. | Small amounts beyond shift end can incorrectly count as overtime, and overnight shift end can be calculated on the wrong date. | Build the correct shift-end datetime, including overnight shifts, and apply the required 30-minute overtime threshold. |
| 11 | Work-hours calculation | Ordinary floating-point rounding does not guarantee the required half-up behavior. | Boundary values can round differently from the contract. | Use `Decimal` with `ROUND_HALF_UP` for work-hour rounding. |
| 12 | Attendance listing | The starter can load records into Python and perform filtering/sorting/pagination there. | Memory and response time grow unnecessarily with the dataset size. | Push filtering, sorting, skip and limit into MongoDB and use `count_documents()` for the total. |
| 13 | Regularization | Updating corrected attendance values without consistently recomputing derived fields can leave inconsistent data. | Corrected punches may no longer agree with work hours, late minutes or overtime. | Validate the correction, recompute derived values, and append the correction details to `history`. |
| 14 | Error handling | Database duplicate/write failures are not consistently translated into API-level errors. | Clients can receive an internal server error for an expected conflict. | Catch relevant PyMongo errors, especially `DuplicateKeyError`, and return the appropriate HTTP response. |

## Reviewed but not considered defects

- Storing attendance `date` and employee `joined_on` as `YYYY-MM-DD` strings is acceptable for this data model because ISO-formatted dates retain chronological ordering for equality and range comparisons.
- A unique `(emp_code, date)` index is intentionally both a correctness constraint and an index; it guarantees one attendance record per employee per attendance date even under concurrent requests.
- Starting department summary analytics from the employee collection is intentional. It allows employees with zero attendance logs to remain part of department headcount.
- MongoDB aggregation is appropriate for the analytics endpoints because the grader dataset is much larger than the supplied sample dataset and the calculations can remain database-side.