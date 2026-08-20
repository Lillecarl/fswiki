# fswiki FUSE client

The wiki as a directory tree on your own machine, so you can edit it with your
editor and point tools at it.

    nix-build .. -A fuse
    eval "$(fswiki-dev env)"
    ./result/bin/fswiki-mount --token "$(fswiki-dev token bob)" ~/wiki

Runs in the foreground on **trio**, which is pyfuse3's native backend — no
asyncio shim. httpx composes with it because httpcore is built on anyio.

### macOS

The Darwin package uses the system-installed FUSE-T runtime and defaults to its
NFS transport. Install FUSE-T's official package first; fswiki expects its
helper at `/Library/Application Support/fuse-t/bin/go-nfsv4`. NFS needs no
kernel extension, Xcode, signing, or filesystem-extension approval:

    nix run --file .. fuse -- --backend nfs \
      --token "$(fswiki-dev token bob)" ~/wiki

Set `--backend fskit` (or `FSWIKI_FUSE_BACKEND=fskit`) to use the optional
FSKit transport on macOS 26+. Install the pre-signed app from the `fuse-t`
output in `/Applications`, then enable **fuse-t** once under System Settings >
General > Login Items & Extensions > File System Extensions. `pluginkit` can
register the extension but cannot grant this FSKit approval.

FSKit officially supports mount points below `/Volumes`; use a path there for
predictable behaviour. FUSE-T currently accepts some paths elsewhere, but that
is outside FSKit's documented mount-point contract.

    nix-build .. -A fuse-t
    cp -R result/Applications/fuse-t.app /Applications/
    mkdir /Volumes/fswiki
    nix run --file .. fuse -- --backend fskit \
      --token "$(fswiki-dev token bob)" /Volumes/fswiki

FUSE-T 1.2.7's NFS named-attribute bridge currently lists fswiki's
`user.fswiki.*` attributes but sends an empty name in the subsequent FUSE
`GETXATTR` request. Ordinary file operations are unaffected; reading fswiki
xattrs through macOS `xattr(1)` is therefore not supported until that upstream
bridge bug is fixed.

| module | |
| --- | --- |
| `naming.py` | filenames <-> ltree slugs, and what counts as scratch |
| `client.py` | PostgREST over httpx; knows nothing about FUSE |
| `model.py` | the manifest with drafts laid over it |
| `inodes.py` | uuid <-> inode, and the kernel's lookup counting |
| `fs.py` | the pyfuse3 operations |

## What you see

`wiki.syncable_document`, which is *not* the same as what you can read. `sync`
is a separate capability, so a document can be perfectly readable in the browser
and absent from the mount — that is the audit lever working, not a bug. Reads go
through the syncable view too, never `current_document`, so there is no path by
which a deny-sync document's body reaches local disk.

One request builds the whole tree; bodies are fetched per open. Directory
listings and `stat` cost nothing after that, because `size` comes down in the
manifest.

Mode bits are a rendering of the ACL, not a second opinion on it: `0444` without
`write`, `0644` with. Every write is judged server-side when the draft is
posted.

## Writing

Writes never touch published history. They land in `wiki.draft` — the working
copy the server already models — and appear in place, so a file you have edited
reads back what you wrote. Publishing is `wiki.push()`, which belongs to the
CLI.

    $ echo '# Notes' > ~/wiki/engineering/notes.md   # a 'create' draft
    $ vim ~/wiki/engineering/onboarding.md           # an 'update' draft
    $ rm ~/wiki/engineering/old.md                   # a 'delete' draft
    $ mv ~/wiki/a/x.md ~/wiki/b/x.md                 # a 'move' draft

### What an edit is based on

Every `update` draft carries a `base_version`, and push refuses the whole
changeset if the server has moved past it. That check is only worth anything if
the number is the revision you actually read, so the mount records it when
content leaves us — at `open()`, before the bytes go out — and not at save time.

Taking it from the tree at save time is a silent lost update. Open a file at
revision 3, someone publishes revision 4, the mount polls and refreshes, you
save: the tree says 4, the draft would claim 4, push would accept it, and
revision 4 would be gone with nobody told. The conflict machinery never runs,
because it was lied to about what was edited.

Three details make it hold up against real editors:

- **A truncating open records nothing.** `O_TRUNC` hands the caller no content,
  so it cannot have shown them a newer revision — and it is exactly what an
  in-place save looks like. Recording there would re-base the edit onto whatever
  the poller last pulled in.
- **The record outlives the file handle.** An atomic save writes a scratch file
  and renames it over the target, so by the time the draft is written the handle
  that read the original is long gone.
- **Your own push is not a conflict.** The CLI publishes out-of-band, so the
  mount only learns of it from the manifest. The tip's author separates the
  cases exactly: if you published it, your copy *is* that revision. Saves
  therefore bypass the poll window before deciding, which costs one
  `change_token()` — a few bytes — unless the wiki has actually moved.

### Scratch files

A name the server cannot hold as a slug — `.foo.md.swp`, `bar.md~`, `#notes#` —
becomes a **local-only file** kept in memory and never sent anywhere.

This is not a workaround, it is what makes editors work at all. vim, emacs and
VS Code save by writing a sibling temp file and renaming it over the target, and
none of those temp names is representable server-side. Handling the rename is
what turns the buffered bytes into a draft.

`mkdir` is local for the same reason: `wiki.push()` materialises every folder on
the path of a document it publishes, so a new directory has nothing to create
until something inside it is pushed.

## Extended attributes

    $ getfattr -d -m . ~/wiki/public/guide/permissions.md
    user.fswiki.capabilities="read,sync"
    user.fswiki.document_id="a2b2fdcf-..."
    user.fswiki.owner_id="c200d43c-..."
    user.fswiki.path="root.public.guide.permissions"
    user.fswiki.state="published"
    user.fswiki.title="How permissions work"
    user.fswiki.version="2"

Read-only for now. `setxattr` returns `ENOTSUP` rather than accepting and
discarding — administering the ACL needs a grammar, and that is the CLI's job.

## Staleness

The manifest is re-fetched every `--ttl` seconds (default 5), and the kernel is
told it may cache entries and attributes for exactly that long — so a warm mount
asks nothing at all between refreshes. Writing forces a refresh immediately, so
your own edits are never stale to you. Someone else's edit takes up to the TTL
to appear.

If a refresh fails, the last good tree is served rather than blanking the mount.

## Audit trail

    fswiki-mount ~/wiki --audit

Off by default. With it, the mount identifies the process behind every open and
every change, and reports it.

    cat[525459]     cat                                        open
    grep[525461]    grep (+2 args)                             open   (x3 documents)
    vim[525604]     vim                                        write

The one-pid-many-opens shape is the useful part: it distinguishes an agent
sweeping the tree from a person opening a page.

Reads go **over POST** while auditing, which is the whole trick. PostgREST runs
GET in a read-only transaction, so a GET cannot record its own access; POST
can, and `wiki.read_document()` returns the body and writes the access event in
one transaction. So an audited read is witnessed by the server rather than
merely reported by the client. Nobody auditing means nobody paying for it —
reads stay on the plain GET.

Anything the fetch cannot carry — a cached body, a draft, a refused open, a
create or delete, a laptop with no network — goes to a local append-only queue
and ships in batches. Both routes mint the event once and share its id, so the
server's `on conflict` collapses them rather than counting twice.

**Command lines are truncated to `argv[0]`.** `mysql -pSECRET` and friends put
other people's credentials in `/proc/<pid>/cmdline`, and none of them are the
wiki's business; what was dropped is counted (`"argv_elided": 3`) so a
truncated command is never mistaken for a bare one. `--audit-argv` sends the
lot, and warns when it does.

Two things bound what this can ever be. `read()` and `write()` carry a file
handle and no caller, so the granularity is opens and saves, never bytes — an
`mmap` shows up as one open and then silence. And the mount runs on the user's
own machine, so `cmdline` and `comm` are forgeable and the whole thing is
telemetry rather than evidence. [docs/audit-trail.md](../docs/audit-trail.md)
has the measurements, the routes that were tried and rejected, and which fields
are worth anything.

## Mounting needs a setuid `fusermount3`

An unprivileged mount needs `CAP_SYS_ADMIN`, and nothing in the Nix store can be
setuid. On NixOS the working binary is `/run/wrappers/bin/fusermount3`, which
requires

    programs.fuse.userAllowOther = true;   # or any option that pulls the wrapper in

Without it the mount fails with `fusermount3: mount failed: Operation not
permitted` even though `/dev/fuse` is present and writable. The wrapper script
therefore puts the store's `fuse3` on the **end** of `PATH`, never the front —
prefixing it shadows the setuid wrapper and produces exactly that error.

For a throwaway test with no system configuration at all, a user namespace works,
because libfuse mounts directly when it believes it is root:

    unshare --user --map-root-user --mount --propagation private -- \
      fswiki-mount ~/wiki

## Known gaps

- Folders cannot be renamed or removed server-side: push has no folder
  restructuring, so `rmdir` on a real folder is `EPERM`.
- A document row with no published revision (see `root.public.unpublished` in the
  dev fixtures) cannot be edited. `wiki.draft`'s shape check demands a
  `base_version` for an update and `publish_revision()` demands it match the live
  revision, which is none — no draft satisfies both. It reads as an empty file
  and writes fail with `EPERM`.
- Creating over a locally-retired document makes a `create` draft, which push
  will report as a conflict. Reinstating should be an update on the tombstone.
- No content cache eviction. Fine for markdown, wrong the day attachments land.
- `--allow-other` is passed through but untested.
