# Files

A picture, a PDF, a spreadsheet. In this wiki a file is not a special kind of
thing beside a document — **it is a revision of one**, whose body happens to be
bytes.

That sentence is the whole design, and everything below follows from it.

## Why a file is a revision

The first shape was a `wiki.attachment` table with the bytes in it, hanging off
a `wiki.document` row. The identity half of that was right and is unchanged: an
attachment *is* a document, so the ACL needs no second implementation, the path
space stays unique, and ownership, inheritance and the audit trail come for
free.

The body half was wrong. Everything a wiki does with a body, this schema
already does through `document_version`:

| what | who does it |
| --- | --- |
| history | `document_version.version` |
| the wiki as of an instant | `document_as_of`, `valid @> t` |
| the sync client's diff | `content_hash` |
| a stat without a fetch | `byte_size` |
| conflict detection | `base_version` |
| removal | `is_tombstone` |
| unpublished work | `wiki.draft` |
| publishing | `wiki.push` |

A separate table means writing a second, worse copy of each. The place that bit
was the mount, which needs every one of them at once — and it is why the table
lasted four migrations.

Two things fall out, and both are improvements rather than consolations:

**`content_type` is already the media type.** `image/png` is a content type the
same way `text/markdown` is. The column that existed for pages is the one a
file needed.

**`document.is_attachment` disappeared.** It existed so `current_document`
could name the kind without joining a table whose policy costs an ACL walk per
row — 0.35 ms each, 138 ms on a 1,420-row manifest holding 400 files. The
version join is already there, so `is_binary` is free and there is no
denormalisation left to drift.

The cost, stated plainly: every revision of a binary keeps its bytes.
`document_version`'s own header warns about exactly that. The cap in
`wiki.setting` bounds each one; external storage, below, is what bounds the
total.

## The columns

```sql
alter table wiki.document_version
  add column content_bytes bytea,
  add column storage text not null default 'database'
    references wiki.storage_backend(name),
  add column locator text,
  add column byte_size bigint;
```

with, in `tables/150_binary_versions.sql`:

- `num_nonnulls(content, content_bytes) <= 1` — one body, or none.
- `storage = 'database'` ⇒ no locator; otherwise a locator and **no local
  bytes**.
- a tombstone has none of the three.
- `byte_size is null` exactly when there is no body at all.

`wiki.draft` gains `content_bytes` and the same one-body rule, because the
mount's promise is that everything you do there is a draft until you push. A
picture that published itself on `cp` would be that promise with an exception
in it, and an exception you cannot see from the filesystem.

### `content_hash` stopped being generated

It was `digest(coalesce(content, ''), 'sha256')`, which cannot see bytes and
cannot see a body that is not in this database at all. A generated column reads
only its own row, so it became an ordinary column that a trigger fills.

The value did not change for existing pages: pgcrypto hashes text in the
database encoding, so on UTF-8 `digest(text, …)` is byte-for-byte
`digest(convert_to(text, 'UTF8'), …)`. `120_binary_test.sql` asserts that
rather than trusting this paragraph, and asserts that every existing page kept
its hash.

## Where the bytes live is a property of the revision

Not of the file. That is deliberate, and it is what makes this open-ended: a
wiki can keep this month's images in the database and last year's in a bucket,
and each revision knows which without anything else moving.

`wiki.storage_backend` is a lookup table, not an enum, and the reason is the
migration chain. `alter type … add value` cannot use the new value in the
transaction that adds it, so adding a backend to an enum is two migrations that
must not be run together. A row is idempotent, it can come from `seed/` instead
of a migration, and an operator can read the list.

Today it holds one row: `database`.

### What an S3 backend would have to fill in

Nothing in the schema. The seam is already cut:

1. **A seed row** naming the backend.
2. **`locator`** — its shape is opaque to the schema on purpose. A column that
   parsed it would be a second place to teach about a backend.
3. **`byte_size` and `content_hash` supplied by the uploader.** The trigger
   *requires* both for external storage and refuses the row without them,
   because nothing in this database ever saw the bytes. That is the only honest
   arrangement.
4. **A way to get a URL.** This is the only genuinely new part, and the
   interesting question is *who signs it*.

On the last point, the answer this project's architecture implies is: **the
database.** It is already the permission engine — RLS decides whether you may
see the row at all — so issuing the download capability from the same place
makes the ACL and the capability one decision rather than two that can drift. A
signing function reading its credentials from `wiki.setting` (granted to no
client role, exactly like the size cap) would hand a time-limited URL to
whoever RLS already let read the revision, and every client — browser, CLI,
mount — would get it by the same route.

The alternative, signing in the browser-server, would leave the CLI and the
mount unable to fetch a file at all without credentials of their own. That is
the shape to avoid.

None of this is written. What is written is that adding it changes no table and
no policy.

## The size limit

`wiki.setting` holds one row and a trigger reads it. Neither half is arbitrary.

**Not a CHECK**, because a CHECK cannot read a limit an operator can change.

**Not a GUC.** `current_setting('fswiki.max_attachment_bytes')` reads from the
*session*, and any role may `SET` a custom GUC in its own session — so a client
could raise its own cap. `wiki.setting` is granted to no client role at all;
only `wiki.max_attachment_bytes()`, which is SECURITY DEFINER, reads it.

**On the table, not in `wiki.attach()`.** psql is a client too.

**And on the draft**, so a file too big to publish is refused when it is
written rather than at push time, with the person long gone.

Text is deliberately not capped by it. A page is bounded by what somebody will
type, and a wiki that refused a long article because of a picture limit would
be absurd.

Configure it with `FSWIKI_MAX_ATTACHMENT_BYTES`, which the server writes into
the row on every migration. An unset variable means *leave it*, so an operator
who raised the cap by hand does not lose it to a restart. A fresh database gets
10 MiB from the seed.

## In the mount

`cp diagram.png ~/wiki/public/` does what `cp notes.md` does: a draft, visible
in `fswiki status`, published by `fswiki push`, and byte-identical coming back.

The FUSE driver was already byte-oriented at both edges — `Client.content()`
returns bytes and the kernel is handed bytes. The obstacle was the text
round-trip in the middle: `data.decode("utf-8", errors="surrogateescape")`
produces lone surrogates, which neither UTF-8 nor JSON can carry back out, so a
picture written through the text column arrived corrupted or not at all.

Which column a body goes in follows from the content type, and
`naming.is_binary_type` is the only place that decides. `test_mount_binary.py`
pushes all 256 byte values through the mount and back to say the split reaches
from the kernel to Postgres.

### The extension maps merged

There used to be two. `parse_filename` decides what a file written into the
mount *means*, and while the mount could not carry bytes, a `logo.png` saved
into a directory had to stay scratch — otherwise it became a document claiming
to be an image with text inside it. Writing one is as meaningful as writing a
page now, so the split had nothing left to protect.

One extension per type, and **no aliases**. `.jpeg` is deliberately absent:
with both, `photo.jpeg` would parse to `image/jpeg` and print back as
`photo.jpg`, and the mount would appear to rename somebody's file after a
refresh. `test_naming.py` asserts the round trip for every entry, which is what
caught it.

### What cannot be done to bytes

`merge` refuses — a three-way merge of two pictures is not a merge, it is
corruption — and `push` still reports the conflict so a person can decide which
copy wins. `diff` reports sizes. `render` says "not a page". Each says so by
name rather than producing nonsense.

## In the browser

A file is served at **its own path**, because the path is the address:

```markdown
![the plan](/public/diagrams/plan.png)
```

Slugs hold no dots, which is what makes the filename mapping reversible, so
`plan.png` is the slug `plan` carrying `.png` exactly as `guide.md` is the slug
`guide`.

There is no second request. The body travels with the revision, which is the
other thing folding it in bought: while the bytes lived in their own table,
`current_document` could not afford to join them, so serving a picture meant
fetching the document and then fetching the file.

### What a browser is told

This is the security surface above the ACL. Bytes one person uploaded, served
from the same origin as everybody else's pages.

Two rules from one set:

- A type in `pages.INLINE` — PNG, JPEG, GIF, WebP, AVIF — is sent as itself
  with `Content-Disposition: inline`. None of them can carry script.
- **Everything else is still sent as its declared type**, but with
  `Content-Disposition: attachment`.

That second rule is what makes an SVG safe without banning it. `<img>` ignores
`Content-Disposition`, so a diagram still draws in the page; a direct visit
downloads the file rather than opening a **document**, which is the only place
an SVG's `<script>` could ever run.

Every file response also carries a policy of its own:

    Content-Security-Policy: default-src 'none'; sandbox; base-uri 'none';
                             form-action 'none'; frame-ancestors 'none'

A second CSP header, not a replacement: a browser enforces every policy it is
given, so two headers intersect and the stricter wins. `sandbox` with no
`allow-scripts` stops a file opened as a document from running anything, before
`Content-Disposition` gets a chance to.

The site-wide policy gained `img-src 'self'`, which is what makes a picture
visible in the page that references it. It admits nothing an author could not
already write: `src` is allowlisted by the sanitiser and a remote host is still
refused, so a page cannot phone home by naming an image on somebody else's
server.

## The bytes are hex in transit

PostgREST renders `bytea` the way Postgres prints it, and JSON has no bytes. So
a body arrives as `\x89504e…` and the client decodes it: about 1.5 ms for a
100 kB image, 150 ms at the 10 MiB cap.

Accepted rather than split into a second request for a scalar column with
`Accept: application/octet-stream`, which would be exact and would also make
every image two round trips instead of one. If a wiki serves large files often,
that is the fix, and it is a change to `fswiki_core.client` alone.

## Using it

    cp diagram.png ~/wiki/public/diagrams/      # through the mount
    fswiki status
    fswiki push -m "the new architecture diagram"

    fswiki attach diagram.png public/diagrams/  # or straight to the wiki
    fswiki attach data.csv public/data/ --type text/csv
    fswiki detach public/diagrams/plan.png

`fswiki attach` publishes immediately; the mount goes through a draft. Both end
up as revisions, and `detach` is a tombstone — its history is kept, so
attaching it again is another revision rather than an apology.

## What it does not do yet

- **No storage backend but the database.** The seam is cut; see above.
- **No dedupe.** `content_hash` is indexed, so the same bytes at two paths are
  findable — but they are two revisions. Deduplicating means a blob table and a
  reference count, and a reference count is a thing that can be wrong.
- **No retention.** Every revision of a binary keeps its bytes, and nothing
  prunes them. The obvious answer is to move old revisions to a bucket, which
  is what per-revision `storage` was designed for.
- **No thumbnails, no transforms, no rendering of any kind.** Bytes in, bytes
  out. Nothing here runs anything.
