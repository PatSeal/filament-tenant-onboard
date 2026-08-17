# Troubleshooting

Every error actually hit while building and running this pipeline, and the real fix — not the first guess.

## Step 1 (extraction / upload)

### `ModuleNotFoundError` / `Missing dependency: pip install pymysql` or `boto3`

```
pip install pymysql boto3 --break-system-packages
```

(or use a virtualenv, if you'd rather not touch system packages).

### `Could not find .env at <path>`

You pointed `--legacy-root` or `--platform-root` at the wrong folder, or that app uses a deployment layout (e.g. a `releases/` + `current` symlink structure) where the `.env` actually lives one level differently than you expect. Confirm the real path with `ls -la` before re-running.

### Media files "not found locally, skipping upload"

The script assumes the default media-library disk convention (`{disk_root}/{media_id}/{filename}`). If your legacy app uses a different storage layout, or if `storage/app/public` is a symlink pointing somewhere else (common with `releases/`+`shared/` deployment layouts), pass `--images-root` explicitly pointing at the real resolved location.

### Uploaded URLs come out with the bucket name doubled (e.g. `.../your-bucket/your-bucket/...`)

Some S3-compatible providers set a "public URL" config value that *already includes* the bucket name in the path (as opposed to the raw API endpoint, which usually doesn't). If your code always appends `/{bucket}/` when building the public URL, you'll double it up whenever the public-URL setting already has it. Fix: detect which case you're in once, and only append the bucket name when the base URL doesn't already include it (`scripts/extract_and_upload.py` does this via the `url_includes_bucket` flag — check it against your actual provider's URL format before trusting it blindly).

### Script refuses to run: "Refusing: --s3-prefix starts with a prefix known to hold real live data"

That's intentional — it's a speed bump, not a real safeguard, meant to stop you from accidentally overwriting production media with staging test data. Point `--s3-prefix` at a clearly-separate staging path instead (e.g. `migration-staging/<tenant-slug>`). If you genuinely mean to write to that prefix, pass `--allow-production-prefix` explicitly.

## Step 2 (import)

### `mkdir(): Permission denied` on most/all media items, even though the target storage folder's own permissions all check out

This is almost always the media library's own **separate temporary staging directory**, not the folder you think you're writing to. Full explanation and fix in `docs/SERVER_PERMISSIONS.md` — short version: add `config(['media-library.temporary_directory_path' => '/tmp/whatever']);` before any media operation runs (already done for you in `scripts/import_tenant_content_standalone.php`), or grant write access to that folder directly.

### Media attaches successfully but you end up with duplicate rows for the same image

The import isn't idempotent for media re-attachment by default — re-running Step 2 (or any ad hoc `addMediaFromUrl()`/`addMedia()` call outside the normal pipeline) can create a second row pointing at the same logical file. See the "Duplicate-media cleanup" section in the main README for the cleanup pattern and its two caveats (don't blindly trust "keep the highest id", and regenerate conversions using the *kept* ids, never the deleted ones).

### A permission warning appeared, but it's unclear if the operation actually failed

Don't assume either way — always check the actual resulting row count/state after any media operation that printed a warning. In the original migration, one `tinker`-run upload printed a permission warning but had actually silently succeeded twice, producing a duplicate instead of an outright failure.

### Import command not found (`Command "migrate:tenant" is not defined`)

You copied `ImportTenantContent.php` into `app/Console/Commands/` but haven't cleared the cached command list:

```
php artisan optimize:clear
php artisan list
```

If it still doesn't appear, confirm the file actually landed in the running app's real folder — if you're deploying via `docker cp` or a similar copy-based workflow, double check you copied into the *currently running* container/release, not a stale one.

### A page "looks migrated correctly" in the database, but images are still broken on the live page

Two distinct failure modes look identical from the outside — check both:

1. **The content model's own media relationship is actually empty or points at a failed upload.** Check directly: `$model->getMedia('default')` (or your package's equivalent) should return real, non-empty entries.
2. **The media is attached correctly, but a *different* model holds raw, unprocessed template text that references the same image by an entirely different id/label scheme** — e.g. a legacy CMS leaving literal placeholder tokens like `{{41|image_url|some-label}}` embedded directly in a stored HTML/CSS blob, which nothing ever substitutes with the real uploaded URL. This is a legacy-CMS-specific gotcha, not something Step 2 introduces — but if your legacy source used a similar templating convention, sweep every content field for the literal pattern (e.g. `{{` for this specific syntax) across every content-bearing model before concluding a site is clean, not just the pages you happened to check by hand. A pattern like this can be genuinely inert on some pages (the current theme just never renders that field) and fully live and broken on others — "dormant where I checked" is not the same as "dormant everywhere."

### A visual check found broken images that an automated `<img>`-only check missed entirely

Some front-end templates render a featured image via CSS `background-image` on a plain element (driven by a dynamically generated `.itemNNN { background-image: url(...) }` rule) rather than a normal `<img>` tag. A check that only tests `<img>` elements (`img.complete && img.naturalWidth > 0`) will never catch this. Check both:

```js
// <img> tags
Array.from(document.querySelectorAll('img'))
  .filter(img => !img.complete || img.naturalWidth === 0);

// CSS background-images (verify each URL actually loads, not just that the rule exists)
Array.from(document.querySelectorAll('*'))
  .map(el => getComputedStyle(el).backgroundImage)
  .filter(bg => bg && bg !== 'none');
// then load each extracted URL via `new Image()` and check it actually resolves
```

### Network-request monitoring shows no image requests on a page that should have images

Browser caching can hide real requests from network-monitoring tools entirely — a cached image loads with no visible network event. Verify via direct DOM inspection instead (see the `<img>` check above), which reflects whether the image actually rendered regardless of whether the request hit the network or the cache.

## General

### A tinker session's variables disappeared

`php artisan tinker` variables do not persist across separate launches of `tinker` — rebuild what you need from scratch each session, and paste multi-line logic as a single-line statement (Psy Shell can mis-parse a statement split across multiple pasted lines).

### Something that worked in a previous debugging session doesn't work now, and you're not sure why

If your working environment resets between sessions (a fresh container, a fresh cloud sandbox, a new SSH session with no persisted shell state), any credential, key, or in-memory override from a previous session is gone — only actual file changes on the server persist. Re-verify current state directly rather than trusting an old note about what's "already there," including specific database ids referenced in old documentation — a row can be deleted or replaced between when a note was written and when it's acted on.
