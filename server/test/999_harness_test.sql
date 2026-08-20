-- The harness, checked against itself. Last file, because it counts the rest.
--
-- One assertion, and it exists because of a bug it would have caught.
--
-- `wiki_test.result` is an ordinary table. An assertion made inside a
-- `begin; ... rollback;` block therefore runs, decides, and has its verdict
-- rolled back with everything else. The suite reports a smaller total and says
-- nothing at all about the difference -- and nobody is counting the total, so
-- nothing looks wrong.
--
-- Five assertions in 080_closure_test.sql lived there. They ran on every pass
-- of this suite and were reported on none of them. They happened to be
-- passing; a failing one would have been just as quiet.
--
-- `wiki_test.attempted` is a sequence, which is the one thing a ROLLBACK does
-- not undo, and `wiki_test.expect()` bumps it before it inserts. So the two
-- numbers agree exactly when every verdict survived to be reported.
--
-- Both are read before this assertion records its own, which is why the
-- expected value is the count and not the count plus one.

do $$
declare
  attempted bigint;
  recorded  bigint;
begin
  select last_value into attempted from wiki_test.attempted;
  select count(*)  into recorded  from wiki_test.result;
  perform wiki_test.expect_eq(
    'every assertion the suite made was recorded', recorded, attempted);
end $$;
