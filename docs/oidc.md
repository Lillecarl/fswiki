# An identity provider

Where a person's identity comes from, and the two joins nobody warns you about.

Built in [nix/module.nix](../nix/module.nix). Every claim below is measured:
ten assertions against a synthetic issuer in
[test/test_oidc.py](../test/test_oidc.py), and a whole machine running dex,
Postgres and `fswiki-serve` as systemd units in
[test/integration.py](../test/integration.py).

## There is no login

Say it first, because everything else reads differently once you know.

A token arrives in an `Authorization` header or an `fswiki_session` cookie, and
that is the whole of it. There is no redirect, no callback, no code exchange:
`SESSION_COOKIE` in [app.py](../server/fswiki_server/app.py) is read already so
that the shape is settled before a login exists, and the browser server is
read-only by construction in any case. So "wire fswiki to your provider" means
*hand it tokens your provider signed* — which a CLI, a mount or a reverse proxy
can do today, and a browser on its own cannot.

## What verifies, and what identifies

Two different parties answer two different questions, and confusing them is the
one mistake that looks like it works.

**PostgREST verifies.** It holds `jwt-secret`, which is a shared secret for a
token this wiki minted, or a JSON Web Key Set for one a provider signed. A
signature it cannot check is a 401, and nothing reaches Postgres.

**Postgres identifies.** `wiki.authenticated_user_id()` resolves the caller by
looking up `(iss, sub)` in `wiki.user_account`. The signature is already somebody
else's problem by then.

That gap is a feature and it is worth naming. A token can be perfectly signed by
a key the wiki trusts and still be **nobody**: a second realm on the same
provider issues a different `iss`, no row matches, and the request is served as
an authenticated stranger who sees exactly what a stranger sees. Not an error.
That is what stops one realm's users from silently becoming another's.

The other direction is the provisioning step, and it is the only thing an
operator has to do by hand:

```sql
insert into wiki.user_account (principal_id, oidc_issuer, oidc_subject, email)
values (…, 'https://idp.example/realms/main', '…the provider''s sub…', …);
```

`sub` is the provider's opaque identifier, not a username. dex's is
`Cgpib2ItYXQtZGV4EgVsb2NhbA`. Keying on the email instead is the tempting
shortcut and it is wrong: email addresses get reassigned, and `(iss, sub)` is
the pair the provider promises is stable.

## The role is a second join, and no provider can make it

PostgREST does `SET ROLE` from a claim. By default that claim is `role`, which
is what [dev/token.py](../dev/token.py) puts there — and no identity provider
will ever put it there, because "the Postgres role to assume" is not a thing an
identity provider knows. It knows who you are and which groups you are in.

So `FSWIKI_JWT_ROLE_CLAIM_KEY` names the claim instead
(`services.fswiki.postgrest.roleClaimKey`), and pointing it at whatever your
provider does emit is the whole of the integration:

```nix
services.fswiki.postgrest.roleClaimKey = ".groups[0]";
```

with the provider putting `fswiki_user` in that group. A group that is not a
database role fails the `SET ROLE` outright rather than falling back, so a claim
key set wrong is loud at the first request instead of quietly downgrading
everybody.

### The trap: a smaller role is not a smaller identity

This is the part that surprises people, and it is pinned in
`test_the_role_claim_does_not_narrow_what_a_token_holder_reads`.

Drop the `role` claim, or set it to `fswiki_anon` outright, and the database
role really does change — `authenticated_user_id()` comes back `42501`, because
only `fswiki_user` holds EXECUTE on it. It changes what the request can read by
**zero rows**. RLS resolves the caller from `iss` and `sub`, which are still
bob's, so wherever the anonymous role holds a table grant the rows that come
back are bob's.

That is [060_roles.sql](../server/schema/tables/060_roles.sql) working exactly as
written — table privileges are the coarse gate, RLS is the fine one — and bob
never sees more than bob. But it means:

> **A request that must be anonymous has to carry no identity, not a smaller
> role.**

A "public" endpoint built by stripping the role claim from a real user's token
is not public. Strip the token.

## Deploying it

[nix/module.nix](../nix/module.nix) does the rest. Two of its decisions are
security rather than plumbing, and both are invisible from inside the program.

**Two database users, one process.** The migrate phase needs DDL. PostgREST must
not have it: an owner is **not subject to RLS**, so a PostgREST connected as the
tables' owner bypasses every policy in `050_rls.sql` while looking perfectly
healthy. They are one OS user making two connections, so peer authentication
needs an ident map — peer maps an OS name to the identical database name and
stops there, and without the map PostgREST is simply refused and never starts.

**The migrate user needs `CREATEROLE`, not only DDL.**
`tables/060_roles.sql` brings `fswiki_anon`, `fswiki_user` and
`fswiki_authenticator` into being, and a role is a cluster object rather than a
database one. Without it the phase dies on "permission denied to create role",
systemd restarts it, and it reads as a wedged service rather than a missing
privilege. Create the three by hand instead if you would rather not grant it:
each is guarded with `if not exists`.

**The key set is fetched, not configured.** `postgrest.jwksFrom` reads it from
the provider before starting and waits for a provider that is still coming up. A
wiki that came up verifying nothing would serve 401s to everyone and look like a
configuration mistake. It is read once, so a rotated key set needs the unit
restarted.

## What is tested, and what is not

`test_oidc.py` stands an RSA key up as an issuer and starts a PostgREST with its
JWK Set. It covers the seam between three parties that never meet — another
issuer's signature, a payload swapped under a good signature, an expired token,
the right key carrying an unregistered `iss`, and the group-claim mapping. It
takes about a second and needs nothing running.

`tests.integration` boots a machine with dex, Postgres and `fswiki-serve` as
units, takes a real password grant, and reads a page with the result. It covers
what the module decides and no in-process test can reach: which user each
connection arrives as, who owns the tables, the fetched key set, and the
ordering, which nothing but systemd arranges.

`tests.browser` drives the half a password grant skips. A real Firefox opens
dex's authorization endpoint, fills in its login form, grants consent, and
follows the redirect; the code becomes a token, the token goes in an
`fswiki_session` cookie, and the wiki serves bob a page it does not serve a
visitor. Both halves are asserted in the same browser, one navigation apart, so
the difference cannot be anything but the cookie.

What that does **not** cover is fswiki starting the flow or handling the
callback, because it does neither — see the first section. The test is the
relying party: it exchanges the code itself. When there is a login, the exchange
moves into the server and the browser half stays as it is.

Two things about that test are worth knowing before you change it.

**The browser runs on the host, not in the guest.** io_uring does not work under
User-Mode Linux — the guest prints `BUG: Bad page map … file:[io_uring]` once
per ring page and then stops answering at all, so the failure arrives as a
timeout naming nothing. Every browser reaches for it, Firefox and Chromium
alike. Running the browser outside is also where a person's browser is anyway.

**The guest's address is fixed rather than handed out.** dex compares a
`redirect_uri` exactly, so the address the browser will use has to be known when
dex is configured, which is before anything boots. Hence `127.0.0.2` in
`test/default.nix` and in dex's `issuer`: dex builds its own absolute URLs from
the issuer, so one naming `127.0.0.1` would send a host browser to its own
loopback.

## Still to come

TLS, and with it a CA the browser trusts. Everything above is `http` on
loopback, which is honest for a test on one machine and is not a deployment. The
part that needs care is that `security.pki.certificateFiles` reaches curl and
anything using OpenSSL but **not** a browser: Chromium reads NSS at
`sql:$HOME/.pki/nssdb` and Firefox keeps its own store, so a browser trusts a
private CA only once `certutil -A` has put it there.
