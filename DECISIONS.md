# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (no duplicates, fast lookups), `joined_on` and `(department, joined_on)` for the summary and trend. `attendance_logs`: unique `(emp_code, date)` (one record per day; also serves the monthly report and trend), `(date -1, emp_code 1)` (attendance list sort and leaderboard month filter) and `(emp_code, punch_in -1)` (punch-out finds the latest open punch). I rejected an index on `status`: it has only five values, so it filters too little to be worth the write cost.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. MongoDB lets only one through because of the unique index; the second gets `DuplicateKeyError`, which I return as 409. There is no read-then-write check, so there is no gap to slip through.

3. **Ties.** I rank with `$rank` (1, 2, 2, 4) and keep everyone whose rank is within `limit`. So tied employees at the cutoff are all returned, even if that gives more than `limit` rows. Cutting one of them would be arbitrary.

4. **Headcount.** The summary starts from `employees`, not logs, then `$lookup`s their logs and `$unwind`s with `preserveNullAndEmptyArrays`, so someone with zero logs still appears once and is counted.

5. **At 100x**, I would precompute daily per-department totals when attendance changes, so the reports read a small summary collection instead of aggregating raw logs.
