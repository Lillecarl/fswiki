-- fswiki: extensions and schema
--
-- Everything lives in the `wiki` schema. `public` is left empty on purpose so
-- that a stray `search_path` never resolves to one of our tables.

create extension if not exists ltree;
create extension if not exists pgcrypto;
-- For the exclusion constraint on document_version.valid: lets a GiST index mix
-- equality on a uuid with range overlap.
create extension if not exists btree_gist;

create schema if not exists wiki;

comment on schema wiki is
  'fswiki content, principals and RBAC. All access is mediated by row-level '
  'security; no client role may read these tables without a policy match.';
