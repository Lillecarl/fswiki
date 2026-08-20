# fswiki CLI

Publishing half of the working copy. The mount records drafts; this turns them
into published revisions.

    nix-build .. -A cli
    eval "$(fswiki-dev env)"
    export FSWIKI_TOKEN=$(fswiki-dev token bob)

    fswiki status                    # what you have pending
    fswiki diff                      # what would change
    fswiki push -m "fix the guide"   # publish all of it
    fswiki push -m "..." a/b.md      # publish a subset
    fswiki push -n -m "..."          # dry run
    fswiki revert                    # what withdrawing your drafts would cost
    fswiki revert --apply a/b.md     # withdraw one
    fswiki render a/b.md             # HTML on stdout
    fswiki preview                   # read it in a browser while you write
    fswiki attach logo.png pub/      # publish a file straight away
    fswiki detach pub/logo.png       # retire one

It depends on `fswiki-core`, not on the FUSE client, so publishing from a server
or a CI job does not require the ability to mount anything.

## Paths

Three forms are accepted, tried in this order:

1. **A file inside a mount.** The FUSE client exposes `user.fswiki.path` as an
   extended attribute, so the exact document path is read off the file rather
   than reconstructed. This is the only form that is certainly right.
2. **An ltree path** — `root.public.welcome` — used as given.
3. **A filesystem path** — `public/welcome.md` — converted by stripping the
   extension and joining with dots.

## Push is all or nothing

If any entry comes back with a status other than `published`, **nothing was
written** and your drafts are exactly where they were. The exit code is 1 and
every entry is printed, not just the first — a changeset can fail on its third
document while the first two looked fine.

    $ fswiki push -m "my edit"
    Push refused: 1 of 1 could not be applied.

        CONFLICT  engineering/onboarding
                  the server is now at revision 4
                  edited from revision 3 but the server is at 4

    Nothing was published and your drafts are untouched.

A conflict means someone published while you were editing.

## Merging

`push()` returns all three sides of a conflict, so the report says whether it is
one worth your attention:

    $ fswiki push -m "my edit"
    Push refused: 1 of 1 could not be applied.

        CONFLICT  engineering/onboarding
                  the server is now at revision 5
                  edited from revision 4 but the server is at 5
                  merges cleanly

    Merge them with: fswiki merge

`fswiki merge` is a dry run unless you pass `--apply`, because merging rewrites
work you have not published yet. With `--apply` it rewrites each conflicting
draft with the merged text and rebases it onto the server's revision, so the
next push is an ordinary one.

    $ fswiki merge --apply
    Merged 1 draft.

          merged  engineering/onboarding

Nothing in it calls `push()`. Push commits the moment every row is publishable,
so using it to *ask* what conflicts would publish the drafts that do not; the
manifest already carries each document's live revision, which is the same
comparison, and the other two sides are plain reads.

### Conflicts are marked, not resolved

Where both sides changed the same lines, the merge leaves markers:

    <<<<<<< yours
    BRAVO-bob
    =======
    BRAVO-frank
    >>>>>>> server

Marker length adapts. Content that already contains a seven-character marker —
a page documenting a merge tool, say — gets eight-character markers around it,
so the nesting stays unambiguous. This is what jj does, for the same reason.

**Two separate guards stop a half-resolved merge being published**, and they
answer different questions.

The *server* refuses any draft still flagged `conflicted`. That flag is set by
the merge and cleared only when the client says the resolution is done, so it
does not depend on anyone's client behaving:

        UNMERGED  engineering/onboarding
                  the merge is unresolved; finish it or back it out

The *client* refuses text that still contains markers. That check has to be
client-side, because the server has no idea what a marker is and a page that
explains them must stay publishable:

    $ fswiki push -m "oops"
    Push refused: 1 draft still contains unresolved conflict markers.

      UNMERGED  engineering/onboarding

Deleting the markers is what makes a draft resolved, so the marker check is how
the client decides to clear the flag — and `push` does that for you. What the
server will not do is take the client's word for a resolution it has not made.

### Backing out

    $ fswiki merge --abort
        restored  engineering/onboarding

The merge kept the text it replaced, so this restores it byte for byte. It is
available whether the merge conflicted or not — a clean merge also rewrote work
you had not published, and you are entitled to change your mind.

`base_version` deliberately does not move when you merge; the rebase happens
when the merge is resolved. So backing out has nothing to undo but the text, and
a draft can never claim to descend from a revision it has not really been
reconciled with.

Published history is never involved in any of this.

## Throwing an edit away

`fswiki revert` withdraws drafts: the file goes back to whatever is published,
and for a draft that creates something the file simply stops existing.

    $ fswiki revert
    2 changes would be withdrawn:

       modified  engineering/onboarding
                 2 changed lines against revision 90
            new  public/brand-new
                 3 lines, published nowhere else

    This discards unpublished work. Nothing keeps a copy of it.
    Withdraw them for real with: fswiki revert --apply

A dry run unless `--apply`, for a stronger reason than `merge` has. `merge
--abort` restores from `pre_merge_content`, a copy the server keeps on purpose.
Revert deletes the draft row, and with it the only copy of that text that ever
existed. There is no undo, so the default is to say what would happen.

The cost is counted against the published text rather than reported as the
draft's size, because the draft's size is not the loss: a 300-line page with
one corrected typo loses one line, and "300 lines" would frighten someone out
of a safe operation. A `delete` or `move` draft discards no text at all, and
says so instead of quoting a number.

A draft in the middle of a merge is flagged, because `merge --abort` will put
that one back and revert will not.

## Rendering

    $ fswiki render public/guide/permissions.md
    <h1>How permissions work</h1>
    ...

Markup to HTML on stdout, so it pipes. `--draft` renders your unpublished
version instead of the published one.

**Which engine is a choice, not a given.** Backends register themselves if
their library is installed, and are picked by the document's `content_type`:

    $ fswiki render --list-backends
      markdown-it-py   4.2.0      text/markdown
      mistune          3.3.3      text/markdown
      plain            1          text/plain

`--backend` names one for a single run; `$FSWIKI_RENDERER` pins one for a
deployment. What is *not* pluggable is anything that decides what a reader's
browser gets: sanitising and wiki-link resolution happen on either side of the
backend, in `fswiki_core.render`, so they cannot vary with the engine somebody
installed.

### Links you may not follow are not links

A wikilink to a document you cannot read renders as plain text, and as exactly
the same plain text as a link to a document that does not exist:

    Visible: <a href="/public/welcome">public/welcome</a>
    Hidden: The Plans
    Absent: Nothing Here

A live link would disclose that the target exists, where it lives and what it
is called, none of which the ACL granted — and it would disclose them in the
HTML, before any click could be audited. Telling "forbidden" from "missing"
*is* the disclosure, so the two are byte-identical.

`--raw` prints what a shared cache would hold instead: links left under the
reserved `/-/fswiki/` prefix, unresolved, because which of them are live is a
property of the reader rather than of the revision. See
[docs/rendering.md](../docs/rendering.md).

## Preview

    $ fswiki preview
    fswiki preview on http://127.0.0.1:8222/
      read-only; ctrl-c to stop

The same pipeline `render` uses, with a shell around it and a URL per page. It
shows your drafts by default — that is what makes it a preview rather than a
view — and `--published` ignores them. It reloads when the wiki changes, by
polling the same eleven-byte change token the mount polls.

**Read-only by construction, not by convention.** Every method other than GET
and HEAD is refused before the request is routed at all, so the property does
not depend on which routes exist today or on nobody adding a form later:

    $ curl -X POST -i http://127.0.0.1:8222/public/welcome
    HTTP/1.1 405 Method Not Allowed
    Allow: GET, HEAD

That is a narrower claim than "safe to expose". `--host 0.0.0.0` binds it to
every interface, which is useful on a remote workstation and worth being clear
about: the server holds *your* token and answers as you, so anyone who reaches
the port reads everything you can read, with no login. It says so at startup
rather than assuming you meant it.

    $ fswiki preview --host 0.0.0.0 --port 4321
    fswiki preview on http://hetztop:4321/
      listening on 0.0.0.0: anyone who can reach this port reads everything
      your token can read, with no login.
      read-only; ctrl-c to stop

An SSH tunnel does the same job without opening a port:

    ssh -L 8222:127.0.0.1:8222 workstation

`http.server` is blocking and the client is async, so the HTTP server runs in a
worker thread and reaches the event loop through an anyio portal — one client,
one connection pool, no second HTTP stack.

## Files

    fswiki attach diagram.png public/diagrams/
    fswiki attach diagram.png public/diagrams/plan.png
    fswiki attach data.csv public/data/ --type text/csv
    fswiki detach public/diagrams/plan.png

A trailing `/` means "into this folder, under its own name", which is what `cp`
does. The media type is guessed from the filename unless `--type` says so.

Or through the mount, which is usually what you want:

    cp diagram.png ~/wiki/public/diagrams/
    fswiki status
    fswiki push -m "the new architecture diagram"

A file is a *revision* of a document, so it behaves like a page everywhere:
`status` shows it, `revert` undoes it, `push` publishes it, and it has history.
`fswiki attach` is the shortcut that skips the draft; the mount does not.

Three things bytes cannot do, each of which says so by name rather than
producing nonsense. `merge` refuses — a three-way merge of two pictures is
corruption, not a merge — though `push` still reports the conflict so you can
decide which copy wins. `diff` reports sizes. `render` says "not a page".

**`detach` retires rather than deletes.** Its history is kept, so attaching it
again is another revision.

The size limit belongs to the wiki, not to this program. The CLI asks for the
number first so that a large file fails in a sentence instead of a round trip,
but the refusal that counts is the database's. See
[docs/attachments.md](../docs/attachments.md).

A page references one by path, the same way it references anything else:

    ![the plan](/public/diagrams/plan.png)

## Known gaps

- `diff` fetches the published body of every selected draft, one request each.
  Fine for a handful, wasteful for a hundred. `merge` is worse: three reads per
  conflicting draft, and `revert` pays it too so its dry run can be accurate.
- `merge` cannot help a create/create collision — there is no common ancestor,
  so the answer is a different name — and it says so rather than guessing.
- `merge --abort` backs out the text, not a `delete`d or `move`d draft's other
  fields. Those operations carry no content to merge, so nothing rewrites them.
- `preview` reloads by polling the change token, so someone else's edit takes
  up to two seconds to appear and your own draft takes as long as the mount's
  own poll. Fine for writing; not a live-typing preview.
- `preview` has no history and no ACL view. It is for reading what you are
  writing.
- `attach` reads the whole file into memory and sends it hex-encoded, which
  doubles it in transit. Fine at the default 10 MiB cap; the fix, if it ever
  matters, is in `fswiki_core.client`.
- No `acl` verbs. `wiki.explain_acl()` is the intended backend and returns the
  ACL in the order it is consulted, including the two rules that skip it — a
  superuser, and an owner's standing `grant`.
