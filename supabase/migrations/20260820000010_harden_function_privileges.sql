-- =============================================================================
-- Close the default PUBLIC execute grant on the foundation functions.
--
-- Postgres grants EXECUTE on a new function to PUBLIC unless told otherwise.
-- Migrations 006, 007 and 009 already account for this -- every function they
-- define is followed by an explicit `revoke ... from public, anon`. Migrations
-- 001 through 004 predate that habit: they grant to `authenticated,
-- service_role` and stop there, which adds a privilege without removing the
-- implicit one underneath it.
--
-- The result, confirmed by the Supabase database linter against the live
-- project, is that seven SECURITY DEFINER functions are reachable by the `anon`
-- role over `/rest/v1/rpc/...`. Two of them matter a great deal:
--
--   create_organization   an unauthenticated caller could create an org
--   create_workspace      ...and a workspace inside one
--
-- Both run as the definer and both call auth.uid(), which is null for anon, so
-- the rows they write would have a null created_by and no owning member -- but
-- the write happens. The other five leak the org/workspace membership graph a
-- uuid at a time to anyone willing to guess ids.
--
-- Fixed here rather than by editing 001-004, because those have shipped: a
-- rewritten migration means a fresh database builds a schema that differs from
-- the one an existing database has.
--
-- Nothing authenticated loses access. Every RLS policy in this schema targets
-- `{authenticated}` only, so the anon role never evaluates the five helper
-- predicates through a policy, and revoking its EXECUTE cannot change any
-- policy outcome.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- The two write RPCs from 003.
-- -----------------------------------------------------------------------------

revoke all on function create_organization(text, text) from public, anon;
grant execute on function create_organization(text, text) to authenticated;

revoke all on function create_workspace(uuid, text, text) from public, anon;
grant execute on function create_workspace(uuid, text, text) to authenticated;

-- -----------------------------------------------------------------------------
-- The membership predicates from 002. These exist to be called from inside RLS
-- policies; that they are also exposed as REST endpoints is incidental, and
-- anon has no business calling any of them.
-- -----------------------------------------------------------------------------

revoke all on function is_org_member(uuid) from public, anon;
grant execute on function is_org_member(uuid) to authenticated, service_role;

revoke all on function has_workspace_access(uuid) from public, anon;
grant execute on function has_workspace_access(uuid) to authenticated, service_role;

revoke all on function org_role_of(uuid) from public, anon;
grant execute on function org_role_of(uuid) to authenticated, service_role;

revoke all on function org_of_workspace(uuid) from public, anon;
grant execute on function org_of_workspace(uuid) to authenticated, service_role;

revoke all on function workspace_of_dataset(uuid) from public, anon;
grant execute on function workspace_of_dataset(uuid) to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- The storage-path helper from 004.
-- -----------------------------------------------------------------------------

revoke all on function try_uuid(text) from public, anon;
grant execute on function try_uuid(text) to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Pin search_path on the three functions that lack it.
--
-- A function with a mutable search_path resolves unqualified names against
-- whatever the caller's search_path happens to be, which is how a SECURITY
-- DEFINER function gets talked into running someone else's `lower()`. None of
-- these three reference a database object -- they raise exceptions, compare
-- OLD/NEW fields and cast to uuid -- so the empty search_path costs them
-- nothing: every operator and type they do use lives in pg_catalog, which is
-- always implicitly searched.
-- -----------------------------------------------------------------------------

alter function reject_mutation() set search_path = '';
alter function raw_uploads_guard() set search_path = '';
alter function try_uuid(text) set search_path = '';
