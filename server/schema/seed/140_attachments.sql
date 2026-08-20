-- The default upload cap.
--
-- `on conflict do nothing` rather than an upsert, because seed/ is replayed on
-- every start and an operator who raised the limit should not find it back at
-- ten megabytes after a restart. The server overwrites it deliberately when
-- FSWIKI_MAX_ATTACHMENT_BYTES is set; nothing else touches it.
--
-- Ten mebibytes is a guess, and it is meant to be one. It is large enough for
-- a screenshot or a slide deck and small enough that a wiki does not become a
-- file server by accident.
insert into wiki.setting (key, value)
values ('max_attachment_bytes', '10485760')
on conflict (key) do nothing;
