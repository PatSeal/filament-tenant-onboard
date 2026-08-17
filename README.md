# Filament Tenant Onboard

Migrate content and media from independent legacy sites into a shared Laravel multi-tenant platform, reading straight from the old database and re-uploading media to S3-compatible storage.

This repo generalizes a real, completed migration (four independent Laravel/Filament sites folded into one shared multi-tenant Filament platform) into a reusable pattern. Every real domain, credential, bucket name, and site-specific detail from that original migration has been replaced with generic placeholders — nothing here is copy-pasteable production data.

## Why this exists

If you're consolidating several independent "single-tenant" Laravel apps into one shared multi-tenant app (a common Filament pattern: one codebase, one database, each customer/organization scoped as a "tenant"), you end up needing to:

1. Read content + media out of each legacy app's own database.
2. Re-upload the media to wherever the new platform actually stores files (local disk, S3, or an S3-compatible provider like IDrive e2, Cloudflare R2, MinIO, etc.).
3. Re-insert the content into the new platform through its real application models, not a raw SQL import, so relationships, casts, and any media library package all work the same way they would through the admin UI.

That's a two-step pipeline, not a one-shot script, because the source and destination almost never share a database engine, a schema, or a storage backend. This repo is that pipeline, plus everything that broke while building it the first time.

## How it works

```
Legacy app's own database  ──(Step 1: extract_and_upload.py)──▶  JSON file + media uploaded to a
(MySQL, one DB per site)                                          staging prefix in S3-compatible storage
                                                                              │
                                                                              ▼
New platform's database  ◀──(Step 2: ImportTenantContent Artisan command)──  reads the JSON, creates/updates
(shared, multi-tenant)                                                       records via real Eloquent models,
                                                                              downloads each media file from its
                                                                              staging URL and re-attaches it via
                                                                              your media library package
```

Step 1 never touches the destination. Step 2 never touches the legacy source. Nothing writes back to the legacy `.env` or legacy database — every legacy read is read-only.

## Repo layout

```
scripts/extract_and_upload.py                  Step 1 — legacy DB + media → JSON + staged uploads
app/Console/Commands/ImportTenantContent.php   Step 2 — Artisan command, JSON → new platform via Eloquent
scripts/import_tenant_content_standalone.php   Step 2, standalone variant (see "Fallback" below)
docs/SERVER_PERMISSIONS.md                     Everything you need from a server admin, including the
                                                one gotcha that will cost you the most time if you skip it
docs/TROUBLESHOOTING.md                        Every error we actually hit, and the real fix for each
LICENSE                                        MIT
```

## Prerequisites

- The new platform is a Laravel + Filament app already set up for multi-tenancy, with some `Tenant` (or `Organization`/`Church`/whatever you call it) model representing each customer, and a media library package (this was built against Spatie's `laravel-medialibrary`, but the pattern applies to any package that stores files under a `{model}/{id}` convention).
- Each legacy site has its own separate database. This repo assumes a schema shaped like `elements` (a generic flexible-content table, self-referencing via `parent_id`), `pages`, `posts`, and a polymorphic `media` table (`model_type` + `model_id`) — adjust the SQL in Step 1 and the field mapping in Step 2 to match your actual legacy schema; the pipeline shape (extract → stage → re-import via Eloquent) is the reusable part, not the exact column names.
- Python 3 with `pymysql` and `boto3` installed (`pip install pymysql boto3 --break-system-packages`, or use a virtualenv).
- Read access to each legacy site's database and its local media files.
- Write access to a staging prefix in your S3-compatible storage (see below — never point this at a prefix your production platform already serves real traffic from).
- Composer + Artisan access on the new platform, to add and run the import command.

## Step-by-step, from the beginning

### 0. Before you touch anything real

- Take a database backup of the new platform. Step 2 wraps everything in a transaction and rolls back on `--dry-run`, but you want a real backup before the first non-dry-run import regardless.
- Confirm the legacy site's schema actually matches what Step 1 expects (`elements`/`pages`/`posts`/`media`, or your adapted equivalents). Run a read-only `SHOW TABLES;` / `DESCRIBE` pass first if you haven't seen this legacy codebase before.
- Pick a staging prefix in your bucket that is obviously not production, e.g. `migration-staging/<tenant-slug>`. Step 1 refuses to write to any prefix starting with `production` or `live` unless you explicitly override it (see the script's `--allow-production-prefix` flag) — this is a deliberate speed bump, not a real security boundary, so don't rely on it as your only safeguard.

### 1. Get credentials to both sides

Two ways to do this, pick whichever fits your setup:

**A. Running directly on a server that hosts both the legacy site and the new platform** (the recommended path if you have SSH access — this is what `scripts/extract_and_upload.py` is written for): point `--legacy-root` and `--platform-root` at each app's own folder on disk, and the script reads each app's own `.env` file directly (view-only — it never writes to either `.env`). You never have to type or relay a single credential by hand.

**B. Running from your own machine against remote databases and remote storage:** pass `--db-host`, `--db-name`, `--db-user`, `--db-password`, `--s3-endpoint-url`, etc. explicitly as flags, and set your new platform's storage credentials as environment variables before running Step 2 (never hardcode them into a file, never commit them). Slower and more manual, but works when you don't have shell access to a shared server.

Either way: use a **read-only** database credential for the legacy side if you can get one. Step 1 never writes to the legacy database, so a read-only user costs you nothing and removes an entire category of risk.

### 2. Step 1 — dry-run the extraction

```
python3 scripts/extract_and_upload.py \
  --site <tenant-slug> \
  --legacy-root /var/www/<legacy-site-folder> \
  --platform-root /var/www/<new-platform-folder> \
  --s3-prefix migration-staging/<tenant-slug> \
  --output /tmp/migration_<tenant-slug>.json \
  --dry-run
```

This connects to the legacy database (read-only) and checks that image paths resolve, without uploading or writing anything. Open the resulting JSON and spot-check a handful of entries — titles, slugs, content, and media entries look right.

### 3. Step 1 — run it for real

Drop `--dry-run` and add `--live` (both are required together — the script always behaves as a dry run unless `--live` is explicitly passed):

```
python3 scripts/extract_and_upload.py \
  --site <tenant-slug> \
  --legacy-root /var/www/<legacy-site-folder> \
  --platform-root /var/www/<new-platform-folder> \
  --s3-prefix migration-staging/<tenant-slug> \
  --output /tmp/migration_<tenant-slug>.json \
  --live
```

Check your storage provider's dashboard to confirm the files actually landed under `migration-staging/<tenant-slug>` (not a production prefix), and that a sample URL from the output JSON loads a real image in a browser.

### 4. Step 2 — add the import command to the new platform

Copy `app/Console/Commands/ImportTenantContent.php` into your platform's own `app/Console/Commands/` folder, adjust the model names/fields to match your real schema (it currently assumes `Tenant`, `Element`, `Page`, `Post` models — rename as needed), then confirm Artisan sees it:

```
php artisan optimize:clear
php artisan list
```

`migrate:tenant` should appear in the list.

### 5. Step 2 — dry-run the import

```
php artisan migrate:tenant /tmp/migration_<tenant-slug>.json --dry-run
```

This parses the JSON, resolves the target tenant, and prints what would be created/updated, then rolls back the transaction — nothing is saved. Confirms the tenant lookup and JSON shape are both correct before anything touches real data.

### 6. Step 2 — run it for real

```
php artisan migrate:tenant /tmp/migration_<tenant-slug>.json
```

**Read `docs/SERVER_PERMISSIONS.md` before this step if you haven't already.** The single most time-consuming problem in the original migration this repo is based on was a media library permission issue that has nothing to do with the folder permissions you'd normally think to check — it's covered in detail there, along with the one-line fix.

Check the printed summary (created/updated counts, media attached vs. skipped). If any media was skipped, see `docs/TROUBLESHOOTING.md`.

### 7. Verify

Open the tenant's real page(s) in a browser and compare against the legacy site side by side. Check both real `<img>` tags and any CSS `background-image` usage — a check that only tests `<img>` elements will miss broken background images entirely (this bit us once; see `docs/TROUBLESHOOTING.md`).

### 8. Repeat for each additional legacy site

The import command matches existing rows (via a `legacy_id`/`legacy_site` pair stashed in a JSON metadata column, or a `(tenant_id, slug)` match where no metadata column exists) rather than blindly inserting, so re-running Step 2 with the same JSON is safe and will update rather than duplicate — useful if you need to correct something and re-import. It is **not** currently idempotent for media re-attachment specifically; see the duplicate-media section below.

## Fallback: standalone script instead of an Artisan command

Some hosting setups don't let you deploy a new file into `app/Console/Commands/` easily (shared/locked-down deployment layouts, no write access to the versioned app folder, etc.). `scripts/import_tenant_content_standalone.php` shows how to get the exact same import behavior by manually bootstrapping Laravel's framework and invoking the Artisan command in-process from a plain PHP script placed anywhere you do have write access:

```
php scripts/import_tenant_content_standalone.php /var/www/<new-platform-folder> /tmp/migration_<tenant-slug>.json --dry-run
```

This also demonstrates a genuinely useful, reusable trick: `Illuminate\Support\Facades\Artisan::call('command:name', [...])` run from a script that has already bootstrapped the framework executes the real command logic in-process — which means a `config([...])` override set earlier in that same script (or the same `tinker` session, if you're doing this interactively) actually takes effect for it. Running `php artisan command:name` fresh at a shell prompt does not get that benefit, since that's a brand new process with none of your session's in-memory overrides. This is exactly how the permission workaround in `docs/SERVER_PERMISSIONS.md` gets applied without ever needing a server admin to change a folder's ACL.

## Duplicate-media cleanup (if Step 2 ever runs more than once against the same tenant)

If an import gets interrupted and re-run, or if you attach media ad hoc outside the normal pipeline (e.g. directly in `tinker`), you can end up with duplicate media rows pointing at the same logical image. Reusable cleanup pattern, run in `tinker` (rebuild the variables fresh each session; paste each line as a single statement):

```php
$modelIds = \App\Models\Element::where('tenant_id', <ID>)->pluck('id');
$allMedia = \Illuminate\Support\Facades\DB::table('media')->where('model_type', 'App\Models\Element')->whereIn('model_id', $modelIds)->orderBy('id')->get(['id', 'model_id']);
$grouped = $allMedia->groupBy('model_id');
$toDelete = [];
$keepIds = [];
foreach ($grouped as $modelId => $rows) {
    if ($rows->count() > 1) {
        $keepId = $rows->max('id');
        $keepIds[] = $keepId;
        foreach ($rows as $row) {
            if ($row->id !== $keepId) { $toDelete[] = $row->id; }
        }
    }
}
foreach ($toDelete as $id) { \Spatie\MediaLibrary\MediaCollections\Models\Media::find($id)?->delete(); }
```

Repeat with `model_type = 'App\Models\Post'` (and any other media-bearing model you have).

Two caveats that cost real time to learn the hard way:

1. **"Keep the highest id" is not always safe.** Before trusting that default, check whether each pair's file extension/size/hash actually match. A genuine re-upload duplicate has near-identical files; if they don't match, one of the "duplicate" rows may actually be a completely different, currently-live image that just happens to share a `model_id`. Cross-check against what's actually rendering on the live page before deciding which id to delete.
2. **After deleting, regenerate any missing image conversions using the *kept* ids, never the deleted ones.** `php artisan media-library:regenerate --ids=<kept ids> --only-missing` — running it with the ids you just deleted is a silent no-op: the command completes with no errors and regenerates nothing, because those rows no longer exist. This exact mistake broke a live homepage hero image in the original migration before it was caught.

If the regenerate command itself hits the permission issue described in `docs/SERVER_PERMISSIONS.md`, run it via `Artisan::call(...)` inside `tinker` instead, with the same config override set first in that session:

```php
config(['media-library.temporary_directory_path' => '/tmp/migration-media-temp']);
\Illuminate\Support\Facades\Artisan::call('media-library:regenerate', ['--ids' => '<comma-separated kept ids>', '--only-missing' => true]);
echo \Illuminate\Support\Facades\Artisan::output();
```

## License

MIT — see `LICENSE`.
