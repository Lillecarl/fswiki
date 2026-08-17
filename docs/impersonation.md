# Impersonation

Letting a maintainer ask "what does Bob actually see?" and get the true answer,
rather than a reconstruction of it.

Everything below was measured against the `fswiki-dev` stack with a working
spike. The spike has been removed; nothing here is committed yet, because one
decision has a real cost and belongs to whoever maintains this.

## Why it is not a convenience

The questions a wiki maintainer gets are all of the form "why can/can't X see
Y", and the tools for answering them are `wiki.explain_acl()` and
`wiki.capabilities_at()`. Those explain the *decision*. They do not show the
*consequence* — a tree with the right things missing from it, a page whose
links go dead halfway down, a mount with a folder that turns out to be empty.

The consequence is what people report and it is what has to be reproduced. So
the feature is not "show me the ACL from another angle", it is **evaluate the
whole system as someone else**.

## One chokepoint

`wiki.current_user_id()` is the only place identity enters. RLS policies,
`can()`, `capabilities_at()`, every view, the mount, the CLI, the preview
server — all of them resolve identity through that one function.

So impersonation goes there, and everything inherits it with no client change
at all. That answers the "impersonate on the FS view, does preview inherit it?"
question in the strongest possible way: **there is nothing to inherit, because
there is only one place that ever decides.** The mount is not special. The
preview server is not special.

The split it needs:

| | |
| --- | --- |
| `wiki.authenticated_user_id()` | the token's principal. Never impersonated. |
| `wiki.current_user_id()` | the effective principal. What everything else asks. |

The real identity must stay reachable, because three things need it: the
impersonation check itself (so impersonation can never be used to grant more
impersonation), the audit trail (so reads are filed against the human), and
anything that refuses to act while impersonating.

Measured: with the spike installed, alice sees 20 documents and bob sees 18.
Alice acting as bob sees **18** — through the ordinary `syncable_document`
endpoint, which is what the mount reads.

## Read-only, and not by remembering to check

Impersonated *writing* must not exist. `document_version.author_id` is
permanent, published history, and an impersonated push would forge authorship
into it irrecoverably. A draft planted under someone's name is nearly as bad.
An audit trail that can be written as someone else is worse than no audit
trail, because it is trusted.

The obvious implementation is to check "am I impersonating?" in every write
path. That is a list, and lists rot: the table added next year will not be on
it.

The database can do it instead:

```sql
perform set_config('fswiki.act_as', v_subject::text, true);
set transaction read only;
```

Measured: with that in place, a write during an impersonated request fails with
`25006 cannot execute INSERT in a read-only transaction`, while the same write
from the same caller without the header succeeds. Nothing downstream knows
about impersonation, and nothing has to.

This is the same shape as the preview server refusing every method but GET
before routing: a property of the system rather than an inventory of the places
it has been remembered.

## Who may act as whom

Not a capability in the document lattice. The lattice answers questions about a
*path*, and impersonation is about an *identity*; `capabilities_at('root.x',
bob)` has no slot for it and should not grow one.

A principal-level grant instead:

```sql
create table wiki.impersonation_grant (
  actor_id   uuid not null references wiki.principal(id) on delete cascade,
  subject_id uuid not null references wiki.principal(id) on delete cascade,
  ...
  constraint impersonation_no_self check (actor_id <> subject_id)
);
```

Both sides expand through `effective_principals`, and that is what makes it
worth doing this way:

- **the actor side** expands so a grant can name the `wiki-admins` group rather
  than each admin;
- **the subject side** expands so a grant naming `everyone` lets an admin act
  as any member, without a row per person.

Groups do **not** come out for free in a second sense, though it looks as if
they do. A group *is* a principal here, so naming a group as the subject is
already expressible — it is simply the wrong answer, measurably and in both
directions — see [Acting as nobody in particular](#acting-as-nobody-in-particular).

### Scoping is the default, not an extra

It is tempting to read the grant table as "admins may be anyone", with a
narrower version as a later refinement. It is the other way round. The table is
`(actor, subject)` pairs, so **a limited grant is the ordinary case and
unlimited is the special one** — a grant whose subject is the `everyone` group.
Restricting impersonation to particular people or groups costs nothing extra,
because there is nothing to add.

What does need care is a different thing, and it is easy to miss.

### You cannot get a partial view of a person

Naming a person means their whole identity, including every group they are in
that has nothing to do with why you were granted the impersonation.

Measured against the dev fixtures, documents readable per principal:

| principal | readable |
| --- | --- |
| `engineering` (the group itself) | **4** |
| `contractors` | 0 |
| `everyone` | 13 |
| carol — in engineering, contractors, everyone | **14** |
| bob — in engineering, everyone | **17** |
| frank — in engineering, everyone | **18** |

So a grant meant to say "you may look into engineering's problems" is not a
four-document grant. Acting as a member of engineering is worth 14 to 18
documents depending on which member is named, and none of those numbers is
engineering's own 4. The scope limits **whom you may name**, never **how much
you get** once you have named them.

The obvious fix — permit acting as a person only when that person's entire
membership is inside the grant — degenerates immediately: everybody is in
`everyone`, so almost nobody would qualify and the feature would not work.

### Two features wearing one name

That measurement splits impersonation cleanly in two, by what is being asked:

**Acting as a person** answers the *support* question — "bob says he cannot see
X". It is intrinsically total for that person, because bob's problem is a fact
about all of bob, and there is no smaller version of it.

**Acting as a membership** answers the *design* question — "what would a regular
office worker see?" — which is the one worth asking **before** granting
anything. It is not the same as acting as a person. It is also not acting as
the group, which is the trap.

### Acting as nobody in particular

The obvious way to build the design half is to name the group as the subject,
since a group is already a principal and `effective_principals` already expands
it. Measured against the dev fixtures, that is wrong in both directions at once.

**It under-reports.** `engineering` reads 4 documents. bob reads 17 — and bob
has **zero** ACEs naming him directly, so every one of those 17 reaches him
through a group. The gap is not something personal to bob. It is that bob is
also in `everyone`, and nobody is ever in only one group.

**It over-reports**, which is worse, because it fails in the direction of
declaring something safe. `engineering` is a member of nothing, so a deny that
names `everyone` never applies to it. The fixtures contain exactly that ACE —
`deny everyone sync` on `root.engineering.secret-plans`. The bare `engineering`
principal may sync that document; **no human in this wiki may**, because every
human is in `everyone`. An impersonation run to check whether something is
hidden would report it visible.

So the unit is not a group, it is a **set of groups** — an ephemeral member of
N groups, belonging to nobody. Measured with a throwaway principal in
`{everyone, engineering}`:

| principal | readable |
| --- | --- |
| `engineering` alone | 4 |
| synthetic `{everyone, engineering}` | **17** |
| bob, who is in exactly those two | **17** |
| documents where they differ | **none** |

Not 17 as a count that happens to match: the same documents, one for one. And
the synthetic principal does not sync `secret-plans`, because the `everyone`
deny reaches it the way it reaches a person.

frank has the same two groups and reads 18. The extra one is `root.io-test`,
where an ACE names frank himself. That is the honest boundary of the feature:
the synthetic principal shows what membership alone is worth, and whatever a
particular person has on top of that stays theirs.

#### The ephemeral user needs no row

It does not have to exist. Creating a real principal and deleting it afterwards
costs writes — which impersonation has just made impossible, since the
transaction is read only — plus cleanup, plus a ghost in every "who is in
engineering?" listing.

Instead the identity lives entirely in the request's own GUC:

```sql
perform set_config('fswiki.act_as_groups', v_groups::text, true);
```

`current_user_id()` returns a synthetic uuid that matches no row in
`wiki.principal`, and `effective_principals()` returns that uuid plus the named
groups, expanded by the usual rule. Everything downstream is then correct
without being told anything:

- `is_superuser()` finds no row, so false;
- ownership never matches, and a hypothetical worker owns nothing;
- `draft.author_id` never matches, so it has no drafts;
- ACE resolution is untouched, because it only ever consults
  `effective_principals`.

Deriving that uuid from the sorted group set rather than at random makes
"acted as {everyone, engineering}" the same subject across requests, so the
impersonation log is comparable to itself.

#### It does not make the gate cheaper

It is tempting to conclude that composing groups you already belong to needs no
grant, since a subset of your memberships ought to give a subset of your view.
Deny ACEs break that: dropping a group can drop a *deny*. The bare-`engineering`
over-report above is precisely that failure at N=1. So a group set is gated like
any other subject — the difference is that its scope is bounded by construction,
which a person's never is.

Worth remembering that the support question often does not need impersonation
at all. `explain_acl()` says *why* in one call. Impersonation earns its keep
when the decision is clear and the **consequence** is not.

### Never step up

```sql
and (wiki.is_superuser(p_actor) or not wiki.is_superuser(p_subject))
```

Without it, one grant naming a superuser is a full privilege escalation
dressed as a diagnostic. Measured: with an explicit `bob -> alice` grant in
place and alice a superuser, bob is still refused — the guard does not depend
on anyone keeping the grant table tidy.

Transitive impersonation is impossible by construction rather than by a check:
`may_impersonate` consults `authenticated_user_id()`, which impersonation never
changes, so an impersonated session is still evaluated as the original human.

## The finding that decides the shape

**A GET cannot record that it was impersonated.**

Impersonation has to leave a trace — an admin reading everyone's private pages
with no record is precisely the abuse the feature invites. The natural place is
the `db-pre-request` hook, which runs inside the request's transaction, before
anything else, and knows both identities. Log there, *then* lock the
transaction down:

```sql
insert into wiki.impersonation_event (actor_id, subject_id, method, path) ...;
perform set_config('fswiki.act_as', v_subject::text, true);
set transaction read only;
```

That works, and it cannot be skipped: the statement that grants the
impersonation is the statement that records it.

It works **on POST**. On GET it does not, because PostgREST has already opened
a read-only transaction before the hook runs, so the hook's own insert fails
with `25006`. Measured both ways: an impersonated `POST /rpc/read_document`
returns `200` and leaves an `impersonation_event`; an impersonated
`GET /syncable_document` fails outright.

This is the same wall [audit-trail.md](audit-trail.md) hit, from the other
side, and it points the same way: **a request that records something is not
idempotent, and POST is the verb for that.** Impersonation records something.
It was never a GET either.

### The decision this leaves

Two coherent positions, and the difference is real work:

**A. Impersonation is POST-only.** A GET carrying the header is refused with a
clear message. Every impersonated read is therefore logged, without exception.
The cost: the mount reads its manifest and drafts over GET, so supporting an
impersonated mount means POST equivalents for those — the same shape as
`read_document`, so it is known work rather than new design. The preview server
and `fswiki render` already read through paths that can be POST.

**B. Impersonation works on GET, unlogged.** No new endpoints. The trace is
then only what a client volunteers, and the client here is the admin's own
machine — which [audit-trail.md](audit-trail.md) already explains is not
evidence. This makes the feature cheap and its accountability decorative.

**Recommendation: A.** The whole justification for building impersonation is
that a maintainer needs it; the whole risk is that it reads everyone's private
pages. Those are the same act, and the only thing separating them is the
record. A version that cannot log itself is the one that will be objected to,
correctly, the first time anyone asks who looked at what.

An unlogged escape hatch is also hard to withdraw later, whereas the POST
endpoints are additive and can land incrementally: preview and `render` first,
where the value is highest and the plumbing already exists, and the mount when
someone actually wants an impersonated filesystem.

## The audit trail under impersonation

`access_event.principal_id` currently comes from `current_user_id()`. Under
impersonation that files the admin's reads against their subject, which is
laundering. It must become `authenticated_user_id()`, with the impersonated
identity in a separate `acted_as` column.

Note that the read-only lockdown blocks `read_document`'s in-band audit insert
as well, so under impersonation the access event has to come from the hook's
own pre-lockdown write, or not at all. That is another argument for the hook
being the thing that logs: it is the only code that runs while the transaction
can still write.

## What this closes

[950_lockdown.sql](../server/sql/950_lockdown.sql) records a known residual: an
authenticated caller can pass an explicit `p_user` to `wiki.can()`,
`wiki.capabilities_at()` and friends and learn another principal's capabilities
on a document whose uuid they know. The file says closing it properly is worth
doing.

Impersonation is what makes closing it possible rather than merely desirable.
Today there is no way to express "this caller is allowed to ask about that
principal", so the choice is between leaving the leak and breaking RLS — the
comment records that revoking the grant makes every policy fail rather than
filter, which was measured. With `may_impersonate()` there is a rule to apply:
`p_user` may be the caller's effective identity, or a principal they hold a
grant over, and anything else raises.

That turns an accidental disclosure into a governed one, which is the same
trade the rest of this design makes.
