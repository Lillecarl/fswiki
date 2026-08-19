{ pkgs ? import <nixpkgs> { } }:

let
  inherit (pkgs) lib;

  postgresql = pkgs.postgresql_18;
  postgrest = pkgs.postgrest;

  # Defaults; every one is overridable from the environment so two checkouts can
  # run side by side.
  defaults = {
    FSWIKI_PG_PORT = "55432";
    FSWIKI_HTTP_PORT = "3000";
    FSWIKI_PC_PORT = "8080";
    FSWIKI_ISSUER = "https://idp.test";
  };

  tokenTool = pkgs.writers.writePython3Bin "fswiki-token"
    {
      libraries = [ pkgs.python3Packages.pyjwt ];
      flakeIgnore = [ "E501" ];
    } (builtins.readFile ./token.py);

  # process-compose drives the whole stack. Every path is an environment
  # variable resolved by the shell that runs the command, so this file stays
  # free of build-time paths and the state directory can move.
  processCompose = (pkgs.formats.yaml { }).generate "fswiki-process-compose.yaml" {
    version = "0.5";

    processes = {
      # initdb, the JWT secret, and nothing that needs a running server.
      init = {
        command = ''
          set -euo pipefail
          mkdir -p "$FSWIKI_STATE/run"

          if [ ! -s "$FSWIKI_STATE/pgdata/PG_VERSION" ]; then
            echo "==> initdb $FSWIKI_STATE/pgdata"
            initdb -D "$FSWIKI_STATE/pgdata" -U postgres --auth=trust >/dev/null
          fi

          # Stable across restarts so tokens minted yesterday still work; it is
          # regenerated only by `fswiki-dev --reset`.
          if [ ! -s "$FSWIKI_STATE/jwt-secret" ]; then
            head -c 48 /dev/urandom | base64 | tr -d '\n' > "$FSWIKI_STATE/jwt-secret"
            chmod 600 "$FSWIKI_STATE/jwt-secret"
            echo "==> minted a dev JWT secret"
          fi
        '';
        availability.restart = "no";
      };

      postgres = {
        # One line on purpose: process-compose strips the newline after a
        # backslash continuation, which leaves the shell an escaped `n` and
        # postgres an argument it cannot parse.
        command = ''exec postgres -D "$FSWIKI_STATE/pgdata" -k "$FSWIKI_STATE/run" -h 127.0.0.1 -p "$FSWIKI_PG_PORT" -c log_min_messages=warning'';
        depends_on.init.condition = "process_completed_successfully";
        readiness_probe = {
          exec.command = ''pg_isready -h 127.0.0.1 -p "$FSWIKI_PG_PORT" -U postgres -q'';
          initial_delay_seconds = 1;
          period_seconds = 1;
          failure_threshold = 30;
        };
        shutdown.signal = 15;
      };

      # Loads the schema exactly once per state directory. Re-running the DDL
      # over a live database would fail on the first `create type`, so absence of
      # the database is the trigger.
      schema = {
        command = ''
          set -euo pipefail
          export PGHOST=127.0.0.1 PGPORT="$FSWIKI_PG_PORT" PGUSER=postgres

          if psql -Atqc "select 1 from pg_database where datname = 'fswiki'" | grep -q 1; then
            echo "==> schema already loaded (fswiki exists); use --reset to rebuild"
            exit 0
          fi

          echo "==> creating database fswiki"
          createdb fswiki

          for f in "$FSWIKI_ROOT"/server/schema/*.sql; do
            echo "    $(basename "$f")"
            psql -q -d fswiki -v ON_ERROR_STOP=1 -X -f "$f"
          done

          # Both files are full of `select helper(...)`, whose result rows are
          # noise; errors still come through on stderr.
          echo "==> seeding"
          psql -q -d fswiki -v ON_ERROR_STOP=1 -X -f "$FSWIKI_ROOT/server/test/010_fixtures.sql" >/dev/null
          psql -q -d fswiki -v ON_ERROR_STOP=1 -X -f "$FSWIKI_ROOT/dev/seed.sql" >/dev/null
          echo "==> ready"
        '';
        depends_on.postgres.condition = "process_healthy";
        availability.restart = "no";
      };

      postgrest = {
        command = ''
          set -euo pipefail
          export PGRST_DB_URI="postgres://fswiki_authenticator@127.0.0.1:$FSWIKI_PG_PORT/fswiki"
          export PGRST_DB_SCHEMAS=wiki
          export PGRST_DB_ANON_ROLE=fswiki_anon
          export PGRST_JWT_SECRET="$(cat "$FSWIKI_STATE/jwt-secret")"
          export PGRST_SERVER_HOST=127.0.0.1
          export PGRST_SERVER_PORT="$FSWIKI_HTTP_PORT"
          export PGRST_DB_POOL=4
          # Runs inside every request's transaction, before anything else, and
          # is the only door into impersonation. See server/schema/100_impersonation.sql.
          export PGRST_DB_PRE_REQUEST=wiki.pre_request
          export PGRST_OPENAPI_MODE=follow-privileges
          exec postgrest
        '';
        depends_on.schema.condition = "process_completed_successfully";
        # An exec probe rather than http_get: process-compose does not expand
        # environment variables in the probe's `port` field, and the port is a
        # runtime knob.
        readiness_probe = {
          exec.command = ''curl -sfo /dev/null "http://127.0.0.1:$FSWIKI_HTTP_PORT/"'';
          initial_delay_seconds = 1;
          period_seconds = 1;
          failure_threshold = 30;
        };
      };
    };
  };

  runner = pkgs.writeShellApplication {
    name = "fswiki-dev";
    runtimeInputs = [
      pkgs.process-compose
      postgresql
      postgrest
      pkgs.coreutils
      pkgs.curl
      tokenTool
    ];
    text = ''
      ${lib.concatStringsSep "\n" (lib.mapAttrsToList
        (k: v: ''export ${k}="''${${k}:-${v}}"'') defaults)}

      # Repo root: the directory holding server/schema. Resolved from $PWD upward so
      # the command works from anywhere inside the checkout.
      if [ -z "''${FSWIKI_ROOT:-}" ]; then
        d=$PWD
        while [ "$d" != "/" ] && [ ! -d "$d/server/schema" ]; do d=$(dirname "$d"); done
        if [ ! -d "$d/server/schema" ]; then
          echo "fswiki-dev: not inside an fswiki checkout (no server/schema above $PWD)" >&2
          echo "            set FSWIKI_ROOT to point at it" >&2
          exit 1
        fi
        FSWIKI_ROOT=$d
      fi
      export FSWIKI_ROOT
      export FSWIKI_STATE="''${FSWIKI_STATE:-$FSWIKI_ROOT/.dev}"

      case "''${1:-up}" in
        --reset|reset)
          echo "==> removing $FSWIKI_STATE"
          rm -rf "$FSWIKI_STATE"
          shift
          ;;
      esac

      case "''${1:-up}" in
        --help|-h|help)
          cat <<EOF
      fswiki-dev [up|reset|url|env|psql|token USER]

        up            start postgres + postgrest under process-compose (default)
        reset         delete $FSWIKI_STATE first, then start clean
        url           print the PostgREST base URL
        env           print shell exports for talking to the stack
        psql [args]   psql into the dev database as postgres
        token USER    mint a JWT for a fixture user (alice bob carol dave erin frank grace)

      Ports come from FSWIKI_PG_PORT ($FSWIKI_PG_PORT), FSWIKI_HTTP_PORT
      ($FSWIKI_HTTP_PORT) and FSWIKI_PC_PORT ($FSWIKI_PC_PORT).
      EOF
          exit 0
          ;;
        url)
          echo "http://127.0.0.1:$FSWIKI_HTTP_PORT"
          exit 0
          ;;
        env)
          echo "export FSWIKI_URL=http://127.0.0.1:$FSWIKI_HTTP_PORT"
          echo "export FSWIKI_STATE=$FSWIKI_STATE"
          echo "export PGHOST=127.0.0.1 PGPORT=$FSWIKI_PG_PORT PGUSER=postgres PGDATABASE=fswiki"
          exit 0
          ;;
        psql)
          shift
          exec psql -h 127.0.0.1 -p "$FSWIKI_PG_PORT" -U postgres -d fswiki "$@"
          ;;
        token)
          shift
          exec fswiki-token "$@"
          ;;
        up)
          # Swallow the literal verb so the rest reaches process-compose.
          shift
          ;;
      esac

      mkdir -p "$FSWIKI_STATE"
      echo "==> state    $FSWIKI_STATE"
      echo "==> postgres 127.0.0.1:$FSWIKI_PG_PORT"
      echo "==> postgrest http://127.0.0.1:$FSWIKI_HTTP_PORT"
      echo

      exec process-compose up \
        --config ${processCompose} \
        --port "$FSWIKI_PC_PORT" \
        "$@"
    '';
  };

in
pkgs.symlinkJoin {
  name = "fswiki-dev-env";
  paths = [ runner tokenTool ];
  meta.description = "Local Postgres + PostgREST stack for fswiki, under process-compose";
}
