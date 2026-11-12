### Changed

- `skmemory-sync@.timer` now runs hourly (was every 6h, and 15 min on some nodes via drop-ins), with a 10 min randomized delay so agents on one node don't start together. `skmemory-sync@.service` runs at `Nice=19`, `IOSchedulingClass=idle`, `IOWeight=10`, `CPUQuota=50%` with a 3h start timeout. On noroc2027's spinning disk a 15-min cadence ran the sync back to back and held IO pressure near 90%.
