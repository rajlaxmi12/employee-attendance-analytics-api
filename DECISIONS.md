# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employees, and a unique compound index on `(emp_code, date)` in attendance to enforce one attendance record per employee per attendance date. I also added indexes supporting date and department-related queries. I considered adding more indexes for every analytics filter, but rejected that because extra indexes increase write and storage cost.

2. **Punch-in race.** If two identical punch-in requests arrive together, both may reach the insert, but MongoDB's unique `(emp_code, date)` index allows only one insert. The other raises `DuplicateKeyError`, which the API converts to HTTP 409. This avoids relying only on a read-before-write check.

3. **Ties.** The leaderboard uses MongoDB `$rank`, so employees with equal total late minutes receive the same rank. This preserves ties rather than assigning an arbitrary sequential rank.

4. **Headcount.** Department summary starts from employees and looks up attendance records. Therefore an employee is still included in headcount even when no attendance log exists for that month.

5. **100x data.** I would pre-aggregate frequently requested analytics into summary collections and update them incrementally. This would reduce repeated large aggregation work while keeping the operational attendance collection focused on writes.