# Attachments

A file that is not a document: an image, a PDF, a spreadsheet. Bytes with a
path, an owner and a media type, which a page can point at and a reader can
fetch.

## An attachment is a document row

Not "has one". Not "looks like one". It **is** a `wiki.document` row, and that
is the whole design.

The obvious alternative — a second table with a `path ltree` of its own —
fails on two counts.

The ACL is keyed on `document.path`, so a separate tree needs the permission
model applied to it a second time, and the second copy of a permission model is
the one that drifts. And `document_path_key` is a unique constraint on **one**
table: two tables holding paths could each hold `root.public.logo`, which is a
page and a file at the same address, and no route or mount can answer that.

Being a document row buys, without a line of new ACL code: inheritance from the
containing folder, per-attachment ACEs, ownership, `inheritance_blocked`,
traversal, one unique path space, and the audit trail. What is left over is the
part that is genuinely different — the bytes — and `wiki.attachment` holds
those.

So the test that matters is not that the attachment policies work. It is that
they **agree with the document's**, for every fixture user, in both directions.
A disagreement one way is a file unreachable to somebody entitled to it; the
other way it has leaked.

### `is_attachment`, and why it is not a join

`current_document` carries a boolean rather than joining to `wiki.attachment`,
and that was measured before it was decided.

| arm | ms |
| --- | --- |
| `current_document`, 1,420 rows, as a reader who sees half | 386 |
| reading 400 attachment rows through `attachment_select` | 138 |

0.35 ms per attachment row, because the policy is `has_capability` — the ACL
walk, not the context form. Joining would add 36% to a view every request
reads, for a column almost no request wants. So the flag lives on the document
row.

That makes it a denormalisation, and a denormalisation that can drift is a page
that renders as an empty file forever. It is maintained by a trigger on
`wiki.attachment`, so it cannot be set to a lie, and a test asserts the
equivalence over every row.

## No history

A page keeps every revision because text is small and a diff is meaningful.
Neither is true here — `document_version`'s own comment warns that full
snapshots stop being free when the content is not markdown, and a five-megabyte
image kept once per edit is exactly that case.

So replacing an attachment overwrites it, and removing one is a delete rather
than a tombstone. That makes `delete` and `purge` the same act, and
`wiki.detach` asks for the stronger of the two rather than offering a `delete`
that turns out to be irreversible.

## The size limit is the database's

`wiki.setting` holds one row and a trigger reads it. Neither half is arbitrary.

**Not a CHECK constraint**, because a CHECK cannot read a limit an operator can
change.

**Not a GUC.** `current_setting('fswiki.max_attachment_bytes')` reads from the
*session*, and any role may `SET` a custom GUC in its own session — so a client
could raise its own cap. `wiki.setting` is granted to no client role at all;
only `wiki.max_attachment_bytes()`, which is SECURITY DEFINER, reads it.

**On the table, not in `wiki.attach()`.** psql is a client too. A limit
enforced only by the upload RPC is a limit with a way round it.

Configure it with `FSWIKI_MAX_ATTACHMENT_BYTES`, which the server writes into
the row on every migration. An unset variable means *leave it*, so an operator
who raised the cap by hand does not find it back at the default because a
server restarted. A fresh database gets 10 MiB from the seed.

The CLI asks the wiki for the number before uploading, so a large file fails in
a sentence rather than a round trip — but the refusal that counts is the
database's, and the error names the cap because a refusal a person cannot act
on is a bug.

## What a browser is told

This is the whole security surface above the ACL. An attachment is bytes one
person uploaded, served from the same origin as everybody else's pages.

Two rules, from one set:

- A media type in `pages.INLINE` — PNG, JPEG, GIF, WebP, AVIF — is sent as
  itself with `Content-Disposition: inline`. None of them can carry script.
- **Everything else is still sent as its declared type**, but with
  `Content-Disposition: attachment`.

That second rule is what makes an SVG safe without banning it. `<img>` ignores
`Content-Disposition`, so a diagram still draws in the page; a direct visit
downloads the file rather than opening a **document**, which is the only place
an SVG's `<script>` could ever run.

Every attachment response also carries a policy of its own:

    Content-Security-Policy: default-src 'none'; sandbox; base-uri 'none';
                             form-action 'none'; frame-ancestors 'none'

A second CSP header, not a replacement: a browser enforces every policy it is
given, so two headers intersect and the stricter wins. `sandbox` with no
`allow-scripts` stops a file opened as a document from running anything, before
`Content-Disposition` gets a chance to.

The media type itself is constrained by the database to RFC 6838
restricted-name characters with one slash and no parameters, so it cannot carry
a semicolon, a space or a newline into a `Content-Type` header. The server never
has to sanitise a value the database could have refused.

The site-wide policy gained `img-src 'self'`, which is what makes an attachment
visible in the page that references it. It admits nothing an author could not
already write: `src` is allowlisted by the sanitiser, and a remote host is still
refused, so a page cannot phone home by naming an image on somebody else's
server.

## Names and routes

An attachment is served at **its own path**, because the path is the address:

    ![the plan](/public/diagrams/plan.png)

Slugs hold no dots — that is what makes the filename mapping reversible — so
`plan.png` is the slug `plan` carrying the extension `.png`, exactly as
`guide.md` is the slug `guide`. The extension comes from the media type.

`naming.from_route` accepts it; `naming.from_display` does not, and the split is
deliberate. A URL ending in `.png` is somebody asking for a file. A **wikilink**
ending in `.tar.gz` should stay literal text, because it names something the
wiki could never hold.

There is a second split for the same reason. `ATTACHMENT_EXT_BY_TYPE` is not
`EXT_BY_TYPE`: the latter holds *document* content types and `parse_filename`
uses it to decide what a file written into the mount means. Put `.png` in there
and a `logo.png` saved into a directory becomes a document claiming to be an
image, with text inside it.

## The mount does not see them

`syncable_document` excludes attachments. One has no revision, so a mirror
would write a zero-byte file where a picture is. Until the FUSE driver can
carry bytes, leaving them out is the honest answer — and it means nothing about
existing mounts changes.

## The bytes are hex in transit

PostgREST renders `bytea` the way Postgres prints it, and JSON has no bytes. So
an attachment arrives as `\x89504e…` and the client decodes it: about 1.5 ms
for a 100 kB image, 150 ms at the 10 MiB cap.

Accepted rather than split into a second request for a scalar column with
`Accept: application/octet-stream`, which would be exact and would also make
every image two round trips instead of one. If a wiki ever serves large files
often, that is the fix, and it is a change to `Client.attachment` alone.

## Using it

    fswiki attach diagram.png public/diagrams/       # into the folder
    fswiki attach diagram.png public/diagrams/plan.png
    fswiki attach data.csv public/data/ --type text/csv
    fswiki detach public/diagrams/plan.png

A trailing `/` means "into this folder, under its own name", which is what `cp`
does. The media type is guessed from the filename unless `--type` says
otherwise.

## What it does not do yet

- **No history.** See above; it is a decision, not an omission.
- **No FUSE.** A binary file in the mount is the obvious next shape, and it is
  the place the "not a document" split gets tested for real: the write path,
  the merge and `fswiki status` all have to know the difference.
- **No dedupe.** `sha256` is generated and indexed, so the same bytes at two
  paths are findable — but they are two rows. Deduplicating means a blob table
  and a reference count, and a reference count is a thing that can be wrong.
- **No thumbnails, no transforms, no rendering of any kind.** Bytes in, bytes
  out. Nothing here runs anything.
