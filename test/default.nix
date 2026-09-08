{
  pkgs ? import <nixpkgs> { },
}:

# The test runner, as a program rather than a shell full of instructions.
#
#   nix run --file . tests                  # everything
#   nix run --file . tests -- -k impersonation -x
#
# Everything the suite shells out to is put on PATH here, so conftest can fail
# with "not on PATH" instead of with whatever a missing initdb looks like three
# layers down.

let
  inherit (pkgs) lib;

  # The checkout, as a build input.
  #
  # `user-mode-nixos` is dropped rather than swept in. It is a checkout of
  # another project that happens to sit here, it is megabytes, and including it
  # would rebuild every suite below whenever a commit landed in it -- for a
  # directory none of them reads. The guest test names what it needs out of it
  # by path, which is a dependency Nix can see.
  src = lib.cleanSourceWith {
    src = ../.;
    filter =
      path: type:
      baseNameOf (toString path) != "user-mode-nixos" && lib.cleanSourceFilter path type;
  };

  fswiki-core = pkgs.callPackage ../core { };
  fswiki-cli = pkgs.callPackage ../cli { };
  fswiki-fuse = pkgs.callPackage ../fuse { };
  fswiki-server = pkgs.callPackage ../server { };
  pyfuse3 =
    if pkgs.stdenv.hostPlatform.isDarwin then
      pkgs.callPackage ../nix/fuse-t/pyfuse3-t.nix { }
    else
      pkgs.python3Packages.pyfuse3;

  python = pkgs.python3.withPackages (ps: [
    ps.pytest
    # The async plugin. anyio, not pytest-asyncio: fswiki_core.client is
    # written against anyio precisely so the same code runs under trio in the
    # mount and under asyncio in the CLI, and only this plugin can run a test
    # under either.
    ps.anyio
    ps.trio
    ps.pytest-xdist
    ps.pytest-cov
    # RSA, for test_oidc.py. The suite signs its own HS256 tokens in twelve
    # lines of hmac and needs nothing for that; an RS256 signature and a JWK's
    # modulus are not something to hand-roll. Not pyjwt: the tokens are still
    # assembled claim by claim where a test can read them, and this only does
    # the arithmetic.
    ps.cryptography
    # For fswiki_fuse.inodes, which is a dict, a Counter and the kernel's
    # lookup-count protocol -- and imports pyfuse3 for one constant. Importing
    # it does not open /dev/fuse, so it works in the sandbox; only mounting
    # needs the device.
    pyfuse3
    fswiki-core
    # The browser-facing server. Unlike the mount and the CLI it is imported
    # in-process rather than run as a subprocess, so it goes in the test
    # environment rather than on PATH.
    fswiki-server
    ps.httpx
    ps.nh3
    ps.markdown-it-py
    ps.mdit-py-plugins
    ps.mistune
    ps.docutils
    ps.latex2mathml
  ]);
  # Just coverage, for the *subprocesses*. The CLI, the mount and the preview
  # server run in their own Nix python environments, so the only way to measure
  # them is to put `coverage` somewhere their interpreter will find it. A
  # dedicated one-package environment rather than the test environment above,
  # which would drop pytest and trio into every child process for no reason.
  coverageEnv = pkgs.python3.withPackages (ps: [ ps.coverage ]);

  runner = pkgs.writeShellApplication {
    name = "fswiki-test";

    runtimeInputs = [
      python
      pkgs.postgresql_18
      pkgs.postgrest
      fswiki-cli
      fswiki-fuse
      # fswiki-serve, for the end-to-end test that runs the real program
      # rather than importing its pieces.
      fswiki-server
    ]
    ++ lib.optionals pkgs.stdenv.hostPlatform.isLinux [
      # getfattr, for the xattrs the mount exposes.
      pkgs.attr
      # fusermount3 is setuid and must come from the system; see fuse/default.nix
      # for why this is a suffix everywhere it appears.
      pkgs.fuse3
    ];

    text = ''
      # The mount's own library, from the package that is actually installed
      # rather than from the source tree beside it. `fswiki_fuse.audit` is
      # ordinary Python -- a queue, a cap and some arithmetic -- and it can be
      # tested without a filesystem, in a build sandbox, at unit-test speed. It
      # imports anyio and fswiki_core and needs no filesystem to run against.
      # The same is true of `model` (folding drafts over a manifest), `inodes`
      # (a bidirectional map with reference counting) and `procinfo` (parsing
      # /proc), and of every module in fswiki_cli that renders rather than
      # fetches. Running those through a subprocess and asserting on a
      # substring of its output tests the substring.
      export PYTHONPATH=${fswiki-fuse}/${pkgs.python3.sitePackages}:${fswiki-cli}/${pkgs.python3.sitePackages}''${PYTHONPATH:+:$PYTHONPATH}

      # FSWIKI_COVERAGE=1 measures the child processes as well as this one.
      #
      # Four of the largest modules in the project -- fs.py, both __main__.py
      # and preview.py -- only ever run in a subprocess, so an in-process
      # coverage run reports them as zero however thoroughly the mount and CLI
      # tests exercise them. That number is worse than no number, because it is
      # the same one an untested module gets.
      #
      # coverage's own answer is COVERAGE_PROCESS_START plus a sitecustomize on
      # the path, which every interpreter runs at startup. Off by default: it
      # rewrites PYTHONPATH for every child and combines at the end, and it is
      # a measurement rather than a test.
      if [ -n "''${FSWIKI_COVERAGE:-}" ]; then
        covdir=$(mktemp -d)
        trap 'rm -rf "$covdir"' EXIT
        echo 'import coverage; coverage.process_startup()' > "$covdir/sitecustomize.py"
        printf '[run]\nparallel = true\nsource_pkgs = fswiki_core, fswiki_cli, fswiki_fuse, fswiki_server\ndata_file = %s/.coverage\n' \
          "$covdir" > "$covdir/coveragerc"
        export PYTHONPATH="$covdir:${coverageEnv}/${pkgs.python3.sitePackages}:$PYTHONPATH"
        export COVERAGE_PROCESS_START="$covdir/coveragerc"
      fi

      root=''${FSWIKI_ROOT:-$PWD}
      while [ ! -d "$root/server/schema" ] && [ "$root" != / ]; do
        root=$(dirname "$root")
      done
      if [ ! -d "$root/server/schema" ]; then
        echo "fswiki-test: not inside an fswiki checkout; set FSWIKI_ROOT" >&2
        exit 1
      fi
      cd "$root"

      if [ -z "''${FSWIKI_COVERAGE:-}" ]; then
        exec pytest "$@"
      fi

      # Not exec, because the combine has to happen afterwards -- and the
      # suite's exit status is kept rather than the report's: a failing run
      # still has a coverage report worth reading, and often that report is
      # what says why.
      status=0
      python -m coverage run --rcfile="$covdir/coveragerc" -m pytest "$@" || status=$?
      python -m coverage combine --rcfile="$covdir/coveragerc" --quiet || true
      python -m coverage report --rcfile="$covdir/coveragerc" --show-missing || true
      exit "$status"
    '';

    meta.description = "Run the fswiki test suite against a throwaway stack";
  };

  # The half of the suite that runs in a build sandbox.
  #
  #   nix build --file . tests.check -L
  #
  # Measured in the sandbox rather than assumed: `lo` is up with 127.0.0.1 and
  # binds and connects fine, so Postgres and PostgREST over loopback need
  # nothing special -- Unix sockets would buy nothing here, though PostgREST
  # does support them (server-unix-socket).
  #
  # What is genuinely absent is `/dev/fuse`. The sandbox's /dev has null, zero,
  # random, tty and little else, and no amount of unsharing conjures a device
  # node that is not there, so the mount tests cannot run in a build at all.
  # `-m 'not mount'` is exactly the line between the two, and that is what the
  # marker is for.
  check =
    pkgs.runCommand "fswiki-check"
      {
        nativeBuildInputs = [ runner ];
        inherit src;
        # httpx builds a default SSL context even for an http:// URL, and looking
        # for a CA bundle that is not there fails with a bare FileNotFoundError
        # from ssl.py -- nothing to do with this suite, but it takes out every test
        # that uses the client.
        SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
      }
      ''
        export FSWIKI_ROOT=$src
        export HOME=$TMPDIR
        fswiki-test -m 'not mount' -p no:cacheprovider
        touch $out
      '';

  # The other half, in a User-Mode Linux guest.
  #
  #   nix build --file . tests.guest -L
  #
  # `check` above stops at /dev/fuse, which a build sandbox does not have and
  # cannot be given. A UML guest is a Linux kernel compiled as an ordinary
  # program, so it brings its own -- and with it the mount tests, inside a pure
  # build, with no KVM, no root and no `unshare` invocation to remember.
  #
  # The guest is root, which is what the mount wants, and Postgres refuses to
  # be. conftest already handles that -- as uid 0 it drops initdb and pg_ctl to
  # uid 1000 -- so the node only has to make uid 1000 a real user, because
  # initdb looks itself up with getpwuid before it does anything else.
  #
  # Nothing is copied into the guest. The store arrives over hostfs, so the
  # runner and the checkout are named in `settings` as store paths and read
  # from where they already are.
  #
  # user-mode-nixos is a submodule at the repository root. FSWIKI_UML points at
  # a checkout somewhere else, which is what you want while changing both at
  # once: an edit there is visible here without a commit, a push and a bumped
  # pointer in between.
  umlSrc =
    let
      elsewhere = builtins.getEnv "FSWIKI_UML";
    in
    if elsewhere != "" then /. + elsewhere else ../user-mode-nixos;

  uml = import (umlSrc + "/lib.nix") { inherit pkgs; };

  guest = uml.mkTest {
    name = "fswiki-mount";
    script = ./guest.py;
    nodes.node = {
      # fusermount3, which fswiki-mount unmounts through. programs.fuse is
      # what puts it under /run/wrappers, and it is opt in on NixOS.
      programs.fuse.enable = true;

      # Postgres, a pytest, and several mounts at once. The 256M default is for
      # a guest that boots and pings.
      boot.uml.memory = "2048M";
      # /tmp is on the root image, and the cluster lives in /tmp.
      boot.uml.diskSize = 4096;

      # Who conftest drops to. isNormalUser rather than a system user: initdb
      # wants a home directory it can write, and a system uid would not be 1000.
      users.users.pg = {
        isNormalUser = true;
        uid = 1000;
      };
    };
    settings = {
      runner = lib.getExe runner;
      root = "${src}";
      # httpx builds an SSL context even for an http:// URL; see `check`.
      certs = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    };
  };

  # The whole thing, deployed.
  #
  #   nix build --file . tests.integration -L
  #
  # A machine running the units a deployment runs -- postgresql, an identity
  # provider, and `services.fswiki` from nix/module.nix -- rather than a stack a
  # fixture built. What that reaches is everything the module decides: peer
  # authentication with one OS user arriving as two database users, a JWKS
  # fetched from a provider that is still starting, and the ordering of the
  # three, which nothing but systemd arranges.
  #
  # dex rather than Keycloak. The question is whether fswiki accepts a real
  # provider's tokens, and dex answers it as one Go binary with its storage in
  # memory: it is up in about a second and costs a few tens of megabytes. A JVM
  # and a realm import would answer the same question a hundred times slower.
  integration = uml.mkTest {
    name = "fswiki-integration";
    script = ./integration.py;
    settings = {
      # The wiki the module deploys is empty: `migrate` loads schema/seed,
      # which builds the root document and the built-in roles and nothing else.
      # There is no bob and no engineering tree in a real deployment, and the
      # test needs somebody to be.
      #
      # These are the two files the rest of the suite uses, named as store paths
      # and read over hostfs, so "bob reads root.engineering" means the same
      # thing here as it does in test_client.py.
      fixtures = "${src}/server/test/010_fixtures.sql";
      seed = "${src}/dev/seed.sql";
    };
    nodes.node = {
      imports = [ ../nix/module.nix ];
      nixpkgs.overlays = [ (import ../nix/overlay.nix) ];

      boot.uml.memory = "2048M";
      boot.uml.diskSize = 4096;

      environment.systemPackages = [
        pkgs.curl
        pkgs.jq
      ];

      # The test looks at the cluster from outside the program -- who is
      # connected, and what they may do -- and it runs as root, which peer
      # authentication maps to the database user `root` and nothing else. sudo
      # is not the way in: a guest has no PAM stack worth the name, and
      # `sudo -u postgres` fails with "authentication service cannot retrieve
      # authentication info" rather than with anything about permissions.
      #
      # An ident map is. Before the module's own, because pg_hba stops at the
      # first match -- and that is also why postgres must be mapped to itself
      # here: the line below is now the one NixOS's own setup scripts match, and
      # a map that named only root would lock them out of their own cluster.
      # They do not fail when that happens, they retry, so it reads as
      # "PostgreSQL Setup Scripts (13s / no limit)" and never ends.
      services.postgresql.identMap = lib.mkBefore ''
        fswiki-test root     postgres
        fswiki-test postgres postgres
      '';
      services.postgresql.authentication = lib.mkBefore ''
        local all postgres peer map=fswiki-test
      '';

      services.fswiki = {
        enable = true;
        postgrest = {
          # dex publishes its keys here, and the unit waits for them.
          jwksFrom = "http://127.0.0.1:5556/dex/keys";
          # An identity provider says which groups somebody is in. Which
          # Postgres role that should become is not a thing it knows, so this
          # is where the two are joined.
          roleClaimKey = ".groups[0]";
        };
      };

      services.dex = {
        enable = true;
        settings = {
          issuer = "http://127.0.0.1:5556/dex";
          # In memory, because this provider exists for the length of one test
          # and a file would only be something to clean up.
          storage.type = "memory";
          web.http = "127.0.0.1:5556";
          oauth2.passwordConnector = "local";
          enablePasswordDB = true;
          staticClients = [
            {
              id = "fswiki";
              name = "fswiki";
              secret = "fswiki-test-secret";
              redirectURIs = [ "http://127.0.0.1:8080/callback" ];
            }
          ];
          staticPasswords = [
            {
              email = "bob@test.invalid";
              # bcrypt of "hunter2". dex takes a hash and never a password, and
              # rejects a cost below 10, so this is what a static user looks
              # like even in a test.
              hash = "$2b$10$LeqL5/urOiX.HE7ftw1xCukjWNePRR/78px/6qWP4xLekq8XxqHHe";
              username = "bob";
              userID = "bob-at-dex";
              # What becomes the `groups` claim, and so -- through
              # roleClaimKey above -- the database role. The second group is
              # here to keep the first from being the only one: `.groups[0]`
              # has to pick, and a list of one would not show that it does.
              #
              # storage.Password carries these and connectors/password.go
              # hands them to the identity verbatim, so a password grant gets
              # them like any other flow. dex's own
              # server_grant_password_test.go asserts exactly that.
              groups = [
                "fswiki_user"
                "engineering"
              ];
            }
          ];
        };
      };
    };
  };
in
# The runner is the default -- `nix run --file . tests` -- with the sandboxed
# build and the two guests hanging off it.
runner // { inherit check guest integration; }
