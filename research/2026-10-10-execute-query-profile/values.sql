-- Edge values for the profile's CSV and preview encoding: NULL, strings that
-- start with a backslash (the string \N among them), commas, quotes, a line
-- break, an empty string, Unicode, the largest UInt64, decimals, floats,
-- booleans, dates and instants in two time zones, an array; and 150 more rows
-- so that a query without a limit is cut at 100.
CREATE DATABASE IF NOT EXISTS ossie_profile;
DROP TABLE IF EXISTS ossie_profile.cells;
CREATE TABLE ossie_profile.cells (
    k UInt32, s Nullable(String), d Date, t DateTime('UTC'),
    t2 DateTime64(3, 'Europe/Berlin'), f Float64, b Bool, n Decimal(10, 2), u UInt64,
    arr Array(UInt8)
) ENGINE = MergeTree ORDER BY k;
INSERT INTO ossie_profile.cells VALUES
    (1, NULL, '2026-01-02', '2026-01-02 03:04:05', '2026-01-02 03:04:05.123', 0.1, true, 1.50, 18446744073709551615, [1, 2]),
    (2, '\\N', '2026-01-03', '2026-01-03 00:00:00', '2026-07-03 00:00:00', 1e20, false, -0.05, 0, []),
    (3, '\\x', '2026-01-04', '2026-01-04 00:00:00', '2026-01-04 00:00:00', -0.0, true, 0, 1, [3]),
    (4, 'a,"b"\nc', '2026-01-05', '2026-01-05 00:00:00', '2026-01-05 00:00:00', 2.5, false, 3, 2, []),
    (5, '', '2026-01-06', '2026-01-06 00:00:00', '2026-01-06 00:00:00', 1, true, 4, 3, []),
    (6, 'Ünïcødé 日本', '2026-01-07', '2026-01-07 00:00:00', '2026-01-07 00:00:00', 1, true, 4, 3, []);
INSERT INTO ossie_profile.cells
SELECT 100 + number, toString(number), '2026-02-01', '2026-02-01 00:00:00',
       '2026-02-01 00:00:00', 1, true, 1, number, []
FROM numbers(150);
