-- 075_changes.sql, the seed half.
-- The file header, and the reasoning, are in ../tables/075_changes.sql.

-- The single row the counter lives in. `on conflict` rather than `default
-- values` because seed/ is replayed on every start, and the table's primary
-- key is a constant -- so a second insert is not a second row, it is an error.
insert into wiki.change_counter (only_row) values (true)
on conflict (only_row) do nothing;
