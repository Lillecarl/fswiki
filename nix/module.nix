# services.fswiki -- the browser-facing server, as a NixOS service.
#
# One unit, because fswiki-serve is one process that supervises another: it
# applies the schema, starts a PostgREST of its own, and then serves. PostgREST
# is not a service here for the same reason it is not one in the program --
# it is an implementation detail of the server, and a deployment that had to
# order two units against each other would be arranging something the program
# already arranges.
#
# Postgres is the opposite. It is infrastructure, it is somebody's data, and it
# outlives any version of this program, so this module uses the NixOS module for
# it rather than shipping one. `database.createLocally` is a convenience for a
# machine that has no Postgres yet; point `database.host` somewhere else and it
# gets out of the way.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.fswiki;
  inherit (lib)
    mkIf
    mkOption
    mkEnableOption
    types
    literalExpression
    ;

  localSocket = cfg.database.host == null;

  # Peer authentication over the socket, and no password anywhere.
  #
  # The two connections are made by one process as one OS user, and they must
  # arrive as two different database users. The migrate phase needs DDL, and
  # PostgREST must NOT have it: an owner is not subject to RLS, so a PostgREST
  # connected as the tables' owner bypasses every policy in 050_rls.sql while
  # looking perfectly healthy. That is the single most expensive mistake this
  # module can make on somebody's behalf, so the two URLs are separate here even
  # though they differ by one word.
  uri = user: "postgres://${user}@/${cfg.database.name}?host=/run/postgresql";
in
{
  options.services.fswiki = {
    enable = mkEnableOption "the fswiki server";

    package = mkOption {
      type = types.package;
      default = pkgs.fswiki-server;
      defaultText = literalExpression "pkgs.fswiki-server";
      description = "The fswiki-server package. See nix/overlay.nix.";
    };

    user = mkOption {
      type = types.str;
      default = "fswiki";
      description = ''
        The OS user the server runs as, and the database user the migrate phase
        connects as. It owns the tables.
      '';
    };

    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Address the browser-facing server binds. Loopback by default: this
        server has no login, so anything it can reach is reachable by whoever
        can reach it. Put a reverse proxy in front rather than widening this.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8080;
      description = "Port the browser-facing server binds.";
    };

    postgrest = {
      package = mkOption {
        type = types.package;
        default = pkgs.postgrest;
        defaultText = literalExpression "pkgs.postgrest";
        description = "The PostgREST the server starts as a child.";
      };

      host = mkOption {
        type = types.str;
        default = "127.0.0.1";
        description = ''
          Address PostgREST binds. Clients -- the CLI and the mount -- talk to
          it directly rather than through the browser server, so a machine
          serving them must widen this and not `host` above.
        '';
      };

      port = mkOption {
        type = types.port;
        default = 3000;
        description = "Port PostgREST binds.";
      };

      jwtSecretFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        example = "/run/fswiki/jwks.json";
        description = ''
          File holding what PostgREST verifies tokens with: a shared secret, or
          a JSON Web Key Set fetched from an identity provider.

          A file rather than a string, because a string in the Nix store is
          world-readable on the machine. It is read at start, so a JWKS that
          rotates needs the unit restarted -- see `jwksFrom`, which fetches one
          before each start.
        '';
      };

      jwksFrom = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:5556/dex/keys";
        description = ''
          A JWKS URL to fetch before starting, instead of naming a file.

          The provider is a dependency of the server rather than of the machine,
          so this waits for it: a wiki whose identity provider is still starting
          must not come up verifying nothing.
        '';
      };

      roleClaimKey = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = ".groups[0]";
        description = ''
          Which claim names the database role, as a JSPath. Null leaves
          PostgREST's default of `.role`.

          An identity provider does not emit `role` and has no way to know what
          it should be: a token says who the holder is and what groups they are
          in. So a deployment with a provider points this at a claim the
          provider does emit, and that mapping is the whole of the integration.
        '';
      };
    };

    database = {
      name = mkOption {
        type = types.str;
        default = "fswiki";
        description = "The database to migrate and serve.";
      };

      host = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Null -- the default -- means the local Postgres over its Unix socket,
          with peer authentication and no password to keep anywhere.

          Set it to reach a Postgres elsewhere, and set `urlFile` too: a remote
          connection needs a password, and a password does not belong in a
          module option where it would land in the store.
        '';
      };

      createLocally = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Turn on `services.postgresql`, and create the database and the user
          that owns it. No effect when `host` names another machine.
        '';
      };

      urlFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          File holding `FSWIKI_DATABASE_URL`, for a Postgres this module did not
          set up. The migrate phase needs DDL rights on it.

          `FSWIKI_POSTGREST_DATABASE_URL` is derived from it by substituting the
          user, so the file must name a URL of the ordinary shape.
        '';
      };
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      example = literalExpression ''{ FSWIKI_RENDER_CACHE_BYTES = "0"; }'';
      description = ''
        Extra environment for the unit. Everything in server/README.md is
        reachable this way; the options above are the ones a deployment cannot
        get wrong quietly.
      '';
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.postgrest.jwtSecretFile != null && cfg.postgrest.jwksFrom != null);
        message = ''
          services.fswiki.postgrest: jwtSecretFile and jwksFrom both set. They
          are two answers to one question -- what PostgREST verifies with --
          and the second would overwrite the first.
        '';
      }
      {
        assertion = localSocket || cfg.database.urlFile != null;
        message = ''
          services.fswiki.database: host names another machine, so urlFile has
          to say how to reach it. A password does not belong in a module option.
        '';
      }
    ];

    services.postgresql = mkIf (cfg.database.createLocally && localSocket) {
      enable = true;
      ensureDatabases = [ cfg.database.name ];
      ensureUsers = [
        {
          name = cfg.user;
          ensureDBOwnership = true;
          # CREATEROLE, because the schema creates roles.
          #
          # server/schema/tables/060_roles.sql brings fswiki_anon, fswiki_user
          # and fswiki_authenticator into being, and a role is a cluster object
          # rather than a database one, so owning the database is not enough:
          # the migrate phase fails on "permission denied to create role" and
          # systemd restarts it forever, which reads as a wedged service rather
          # than as a missing privilege.
          #
          # A deployment that will not grant this can create the three roles by
          # hand -- the file guards each with `if not exists`, so it then does
          # nothing -- and set `database.createLocally = false`.
          ensureClauses.createrole = true;
        }
      ];

      # One OS user, two database users. `ensureUsers` cannot say this and
      # neither can peer authentication on its own: peer maps an OS name to the
      # identical database name and stops there, so without a map the
      # authenticator connection is refused and PostgREST never starts.
      #
      # fswiki_authenticator is created by the schema rather than here -- see
      # server/schema/tables/060_roles.sql -- which is why it is not in
      # ensureUsers: it does not exist until the migrate phase has run once, and
      # ensureUsers would fail the first boot.
      identMap = ''
        fswiki ${cfg.user} ${cfg.user}
        fswiki ${cfg.user} fswiki_authenticator
      '';
      authentication = lib.mkBefore ''
        local ${cfg.database.name} ${cfg.user},fswiki_authenticator peer map=fswiki
      '';
    };

    users.users = mkIf (cfg.user == "fswiki") {
      fswiki = {
        isSystemUser = true;
        group = "fswiki";
        description = "fswiki server";
      };
    };
    users.groups = mkIf (cfg.user == "fswiki") { fswiki = { }; };

    systemd.services.fswiki = {
      description = "fswiki: the wiki, in a browser";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ] ++ lib.optional localSocket "postgresql.target";
      requires = lib.optional (cfg.database.createLocally && localSocket) "postgresql.target";

      path = [ cfg.postgrest.package ] ++ lib.optional (cfg.postgrest.jwksFrom != null) pkgs.curl;

      environment = {
        FSWIKI_HOST = cfg.host;
        FSWIKI_PORT = toString cfg.port;
        FSWIKI_POSTGREST_HOST = cfg.postgrest.host;
        FSWIKI_POSTGREST_PORT = toString cfg.postgrest.port;
        FSWIKI_POSTGREST_BIN = lib.getExe cfg.postgrest.package;
      }
      // lib.optionalAttrs localSocket {
        FSWIKI_DATABASE_URL = uri cfg.user;
        FSWIKI_POSTGREST_DATABASE_URL = uri "fswiki_authenticator";
      }
      // lib.optionalAttrs (cfg.postgrest.roleClaimKey != null) {
        FSWIKI_JWT_ROLE_CLAIM_KEY = cfg.postgrest.roleClaimKey;
      }
      // cfg.environment;

      # The secret is read into the environment at start rather than named as a
      # file, because that is the interface the program has: FSWIKI_JWT_SECRET
      # is a value. Reading it here keeps it out of the store and out of
      # `systemctl show`, which prints Environment= but not what a script read.
      script =
        lib.optionalString (cfg.postgrest.jwksFrom != null) ''
          # The provider is a dependency of this server, not of the machine, so
          # wait for it. A wiki that came up verifying nothing would serve 401s
          # to everyone and look like a configuration mistake.
          until keys=$(curl -sSf --max-time 5 ${lib.escapeShellArg cfg.postgrest.jwksFrom}); do
            echo "waiting for the identity provider at ${cfg.postgrest.jwksFrom}"
            sleep 1
          done
          export FSWIKI_JWT_SECRET="$keys"
        ''
        + lib.optionalString (cfg.postgrest.jwtSecretFile != null) ''
          FSWIKI_JWT_SECRET=$(cat ${lib.escapeShellArg (toString cfg.postgrest.jwtSecretFile)})
          export FSWIKI_JWT_SECRET
        ''
        + lib.optionalString (!localSocket) ''
          FSWIKI_DATABASE_URL=$(cat ${lib.escapeShellArg (toString cfg.database.urlFile)})
          export FSWIKI_DATABASE_URL
          FSWIKI_POSTGREST_DATABASE_URL=''${FSWIKI_DATABASE_URL/\/\/${cfg.user}@/\/\/fswiki_authenticator@}
          export FSWIKI_POSTGREST_DATABASE_URL
        ''
        + ''
          exec ${lib.getExe cfg.package}
        '';

      serviceConfig = {
        User = cfg.user;
        Group = cfg.user;
        Restart = "on-failure";
        RestartSec = "2s";
        StateDirectory = "fswiki";

        # The server reads a schema out of its own store path, talks to two
        # sockets and writes nothing. Everything below follows from that; the
        # one thing it genuinely needs is to reach Postgres and, with jwksFrom,
        # the provider.
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged @resources"
        ];
        UMask = "0077";
      };
    };
  };
}
