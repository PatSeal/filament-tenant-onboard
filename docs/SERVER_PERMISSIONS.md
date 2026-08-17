# Required server permissions

Everything below is what you actually need to grant (or get granted) for this pipeline to work end to end. One of these — the media library temp directory — is not obvious, is not where you'd normally think to look, and cost more debugging time than everything else in this project combined. Read that section before your first real Step 2 run, not after it fails.

## 1. Filesystem — legacy site (read-only)

Step 1 needs to read, but never write:

- The legacy app's `.env` file (to get database credentials, if using the co-located server approach).
- The legacy app's media storage folder (commonly `storage/app/public/` in a Laravel app, or wherever your media library package's `public` disk actually resolves to on disk — confirm this isn't a symlink pointing somewhere unexpected before assuming the default path).

A read-only Unix user, or even just read permission on these two paths for whatever user runs the Python script, is sufficient. Nothing in Step 1 opens either of these paths for writing.

## 2. Filesystem — new platform (read + write)

Step 2 needs the running PHP process to be able to write into wherever your media library package actually finalizes uploaded files. For Spatie's `laravel-medialibrary` on the `public` disk, that's normally `storage/app/public/{media_id}/{filename}`.

**Standard Laravel/Unix permission checklist**, if media attachment fails and you haven't confirmed these yet:

- The web/CLI user (whichever user actually runs `php artisan` or your standalone script) has write + execute permission on `storage/app/public/` and every subfolder it needs to create.
- `storage/app/public` → `public/storage` is correctly symlinked (`php artisan storage:link`) if you're serving these files publicly.
- No `open_basedir` PHP restriction blocks the target path.
- No filesystem immutable flag (`lsattr`) is set on the target folder.
- Unix folder permissions do **not** cascade to newly-created nested subfolders automatically — if your media library creates a new `{id}/` folder per upload, the *parent* folder's permissions (and default ACL, if using `setfacl -d`) are what actually govern whether that new subfolder is writable, not a one-time recursive `chmod` you ran once in the past.

## 3. The one that will actually get you: the media library's own temporary staging directory

This is the gotcha. If media attachment fails with something like:

```
mkdir(): Permission denied in vendor/spatie/temporary-directory/src/TemporaryDirectory.php on line 94.
file_put_contents(storage/media-library/temp/...): Failed to open stream: No such file or directory
```

...even though `storage/app/public/` checks out as fully correct on every test above (a plain `mkdir` there succeeds, `getfacl` shows the right permissions, the running PHP UID matches, `open_basedir` is empty, no immutable flags) — this is why.

**The actual cause:** Spatie's Media Library (and likely other media packages with a similar staging step) doesn't write directly into its final storage location. It first stages and processes the file in a **separate temporary working directory** — for Spatie, that's `storage/media-library/temp/` by default — and only moves the finished result to its real home afterward. This folder is easy to miss entirely, because it isn't the folder your upload is supposedly going into, so it's not the first (or fifth) place anyone checks.

**How to confirm this is actually your problem:** reproduce the failing operation directly in `tinker`, without wrapping it in a try/catch, so you see the real unfiltered exception instead of any simplified warning message your own import code might print:

```php
$model = \App\Models\Element::first();
$model->addMedia('/tmp/some-test-image.jpg')->toMediaCollection('default');
```

If the real exception mentions `storage/media-library/temp` (or your package's equivalent staging path), you've confirmed it.

**Two ways to fix it — pick one:**

**Option A — runtime config override (no server admin needed, works immediately, and is what this repo's scripts do by default):** redirect the package's temp staging to somewhere already world-writable, like `/tmp`, via a one-line config override applied before any media operation runs:

```php
config(['media-library.temporary_directory_path' => '/tmp/migration-media-temp']);
```

`scripts/import_tenant_content_standalone.php` sets this immediately after bootstrapping Laravel, so every import run through that script picks it up automatically. If you're instead running the Artisan command directly (`php artisan migrate:tenant ...`) or doing ad hoc work in `tinker`, this override does **not** carry over automatically — you need to set it explicitly at the start of that same process/session, or add it permanently (see Option B).

**Option B — permanent fix, requires a server admin:** grant write access to the actual temp folder itself, and make sure new subfolders inherit it (this is the folder people usually forget to include when granting media/storage permissions, since it's not under `storage/app/`):

```bash
setfacl -R -m u:<app-user>:rwx /path/to/app/storage/media-library/
setfacl -R -d -m u:<app-user>:rwx /path/to/app/storage/media-library/
```

Either fix is sufficient on its own; Option A is faster to apply yourself and doesn't require touching server ACLs at all, which is why it's the default in this repo.

**Important caveat:** a permission warning from a media operation does not reliably mean the operation actually failed. In the original migration, one ad hoc `addMediaFromUrl()` call in `tinker` printed a permission warning but had actually silently succeeded *twice*, creating a duplicate row instead of erroring cleanly. Always verify the resulting media count/rows after any tinker-run media operation, warning or not.

## 4. Database privileges

**Legacy side:** request a read-only credential if at all possible. Step 1 never issues a write query, so a read-only user costs nothing and removes an entire class of risk if the script (or its credentials) is ever misused.

**New platform side:** the account running Step 2 needs normal read/write access to the application's own database — nothing more. Watch out for over-broad grants here: if you're handed a credential that turns out to have superuser-level access to the *entire* database server (every database, `CREATE USER`, `SHUTDOWN`, `WITH GRANT OPTION`, etc.) rather than being scoped to just this app's database, that's worth flagging back to whoever issued it. It's a real risk even if nothing goes wrong, and it's a much smaller ask to fix before a migration than after.

## 5. Storage (S3-compatible bucket) credentials

You need, at minimum:

- Access key + secret key scoped to write access on a staging prefix (ideally not the same credential your production platform uses for its live prefix).
- The provider's API endpoint URL.
- The public base URL used to build browser-loadable links, if it differs from the raw API endpoint (some providers, when their public URL setting already includes the bucket name in the path, will double up the bucket name if your code naively appends it again — check a sample generated URL against what's actually reachable in a browser before trusting it).

If you're using the co-located "read straight from each app's own `.env`" approach (see the README), you likely already have these sitting in the new platform's own `.env` and don't need to request them separately at all.

## Summary checklist

- [ ] Read access to the legacy app's `.env` and media folder (or equivalent credentials, if not co-located).
- [ ] Write access to the new platform's media storage folder (`storage/app/public/` or equivalent).
- [ ] Either the `storage/media-library/temp/`-equivalent folder is writable, **or** the runtime config override is applied (this repo's scripts do the latter by default).
- [ ] A scoped (not superuser) database credential for the new platform's own database.
- [ ] A read-only (ideally) database credential for the legacy site.
- [ ] A staging-scoped storage credential and a confirmed-correct public URL format.
