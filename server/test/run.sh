#!/usr/bin/env bash
# Load the schema into a throwaway cluster and run the ACL/RLS tests.
#
#   ./server/test/run.sh                 # uses $PGBIN or whatever is on PATH
#   PGBIN=/nix/store/...-postgresql/bin ./server/test/run.sh
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/.." && pwd)

PGBIN="${PGBIN:-}"
if [[ -n "$PGBIN" ]]; then
  export PATH="$PGBIN:$PATH"
fi

port="${PGPORT:-55432}"
datadir="${PGDATA_DIR:-$(mktemp -d)/pgdata}"

cleanup() {
  pg_ctl -D "$datadir" stop -m immediate >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> initdb in $datadir"
initdb -D "$datadir" -U postgres --auth=trust >/dev/null

echo "==> starting postgres on 127.0.0.1:$port"
pg_ctl -D "$datadir" -o "-h 127.0.0.1 -p $port -k ''" -l "$datadir/server.log" start >/dev/null

psql() { command psql -h 127.0.0.1 -p "$port" -U postgres -v ON_ERROR_STOP=1 -X "$@"; }

psql -q -c 'create database fswiki'

echo "==> loading schema"
for f in "$root"/schema/*.sql; do
  echo "    $(basename "$f")"
  psql -q -d fswiki -f "$f"
done

echo "==> loading fixtures and tests"
for f in "$here"/0*.sql; do
  echo "    $(basename "$f")"
  # Assertions report through wiki_test.result; the per-statement output is noise.
  psql -q -d fswiki -f "$f" >/dev/null
done

echo
psql -d fswiki -P pager=off -c "
  select case when ok then 'PASS' else 'FAIL' end as status,
         label,
         case when ok then null else detail end as detail
    from wiki_test.result
   order by seq"

failed=$(psql -d fswiki -Atc 'select count(*) from wiki_test.result where not ok')
total=$(psql -d fswiki -Atc 'select count(*) from wiki_test.result')
echo
echo "==> $((total - failed))/$total passed"
[[ "$failed" == "0" ]]
