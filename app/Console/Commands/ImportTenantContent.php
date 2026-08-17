<?php

namespace App\Console\Commands;

use App\Enums\PageType;
use App\Enums\PostStatus;
use App\Models\Tenant;
use App\Models\Element;
use App\Models\Page;
use App\Models\Post;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;

/**
 * migrate:tenant
 *
 * Step 2 of the tenant-onboarding migration pipeline. Reads the JSON
 * file produced by scripts/extract_and_upload.py (grouped
 * elements/pages/posts, each with a "media" array of already-uploaded
 * files) and imports it into this tenant's own slot on the new
 * platform, using the app's real Eloquent models so relationships,
 * casts, and your media library package all work the same way they
 * would through the admin panel.
 *
 * ADAPT TO YOUR OWN SCHEMA: this command assumes a `Tenant` model plus
 * three content model shapes (`Element`, `Page`, `Post`) matching a
 * generic legacy CMS layout. Rename/restructure freely -- the pipeline
 * shape (read JSON, resolve tenant, upsert via Eloquent, re-attach
 * media from a staged URL) is what's reusable, not these exact class
 * names.
 *
 * IDEMPOTENCY NOTE: matching legacy rows to already-imported rows (so
 * a second run updates instead of duplicating) needs *some* stable
 * key. Two patterns are used here depending on whether the model has
 * a free-form JSON `metadata` column to stash a `legacy_id` in:
 *   - elements/posts: legacy_id + legacy_site are stashed inside the
 *     `metadata` JSON column and matched on for updates.
 *   - pages: no `metadata` column in this example schema, so matched
 *     on (tenant_id, slug) instead.
 * If your own schema has a dedicated `legacy_id` column, use that
 * directly instead of a JSON metadata trick.
 *
 * CONTENT/METADATA NOTE: extract_and_upload.py's JSON has `content`
 * and `metadata` as JSON-ENCODED STRINGS (PyMySQL returns MySQL JSON
 * columns as raw strings, not decoded arrays). These must be
 * json_decode()'d here before assigning to the model -- an Eloquent
 * `array` cast will json_encode() whatever it's given, so handing it
 * an already-encoded string would double-encode it.
 *
 * PARENT_ID NOTE: legacy `parent_id` values refer to another legacy
 * element's OLD id, not any id in this database. Elements are
 * imported in two passes: first all elements are created/updated
 * (without parent_id), building a legacy_id -> new_id map; a second
 * pass then sets parent_id using that map.
 *
 * NOT YET CONFIRMED FOR YOUR SCHEMA: what real values your own legacy
 * `pages.type` / `posts.status` columns contain. The mapping below
 * defaults defensively (type -> a generic HTML type, status -> draft)
 * when the legacy value doesn't cleanly match -- confirm against real
 * legacy data before a production run, so nothing is silently
 * mis-published.
 */
class ImportTenantContent extends Command
{
    protected $signature = 'migrate:tenant {json_path} {--dry-run}';

    protected $description = "Import a tenant's migrated content (elements/pages/posts + media) from extract_and_upload.py's JSON output";

    private array $elementLegacyIdToNewId = [];

    private array $elementLegacyIdToParentLegacyId = [];

    public function handle(): int
    {
        $jsonPath = $this->argument('json_path');
        $dryRun = (bool) $this->option('dry-run');

        if (! is_file($jsonPath)) {
            $this->error("JSON file not found: {$jsonPath}");

            return self::FAILURE;
        }

        $data = json_decode(file_get_contents($jsonPath), true);

        if (json_last_error() !== JSON_ERROR_NONE) {
            $this->error('Invalid JSON: '.json_last_error_msg());

            return self::FAILURE;
        }

        $tenantSlug = $data['site'] ?? null;

        if (! $tenantSlug) {
            $this->error('JSON file has no "site" field to identify the tenant.');

            return self::FAILURE;
        }

        $tenant = Tenant::where('slug', $tenantSlug)->first();

        if (! $tenant) {
            $this->error("No tenant found with slug \"{$tenantSlug}\". Create the tenant first.");

            return self::FAILURE;
        }

        $this->info("Importing into tenant: {$tenant->name} (id={$tenant->id}, slug={$tenant->slug})".($dryRun ? ' [DRY RUN]' : ''));

        DB::beginTransaction();

        try {
            $counts = ['elements' => 0, 'pages' => 0, 'posts' => 0, 'media' => 0, 'media_skipped' => 0];

            $this->importElements($data['elements'] ?? [], $tenant, $tenantSlug, $dryRun, $counts);
            $this->linkElementParents($tenant, $dryRun);
            $this->importPages($data['pages'] ?? [], $tenant, $dryRun, $counts);
            $this->importPosts($data['posts'] ?? [], $tenant, $tenantSlug, $dryRun, $counts);

            if ($dryRun) {
                DB::rollBack();
                $this->info('Dry run complete. No changes were saved (transaction rolled back).');
            } else {
                DB::commit();
                $this->info('Import complete.');
            }

            $this->table(
                ['Type', 'Count'],
                [
                    ['Elements', $counts['elements']],
                    ['Pages', $counts['pages']],
                    ['Posts', $counts['posts']],
                    ['Media attached', $counts['media']],
                    ['Media skipped (no URL / failed)', $counts['media_skipped']],
                ]
            );
        } catch (\Throwable $e) {
            DB::rollBack();
            $this->error('Import failed, all changes rolled back: '.$e->getMessage());

            return self::FAILURE;
        }

        return self::SUCCESS;
    }

    /**
     * Pass 1: create/update every element (parent_id left untouched here --
     * legacy parent_id values refer to OLD ids, not anything in this DB yet).
     */
    private function importElements(array $items, Tenant $tenant, string $tenantSlug, bool $dryRun, array &$counts): void
    {
        foreach ($items as $item) {
            $counts['elements']++;
            $this->line(" - element legacy_id={$item['legacy_id']} key={$item['key']}");

            if (! empty($item['parent_id'])) {
                $this->elementLegacyIdToParentLegacyId[$item['legacy_id']] = $item['parent_id'];
            }

            if ($dryRun) {
                continue;
            }

            $existing = Element::withoutGlobalScope('tenant')
                ->where('tenant_id', $tenant->id)
                ->where('metadata->legacy_id', $item['legacy_id'])
                ->where('metadata->legacy_site', $tenantSlug)
                ->first();

            $metadata = $this->decodeJsonField($item['metadata'] ?? null);
            $metadata['legacy_id'] = $item['legacy_id'];
            $metadata['legacy_site'] = $tenantSlug;

            $attrs = [
                'tenant_id' => $tenant->id,
                'key' => $item['key'],
                'slug' => $item['slug'],
                'content' => $this->decodeJsonField($item['content'] ?? null),
                'is_active' => (bool) $item['is_active'],
                'metadata' => $metadata,
            ];

            $element = $existing ?: new Element;
            $element->fill($attrs);
            $element->tenant_id = $tenant->id; // fill() alone may not bypass auto-scoping logic reliably in console context
            $element->save();

            $this->elementLegacyIdToNewId[$item['legacy_id']] = $element->id;

            [$attached, $skipped] = $this->attachMedia($element, $item['media'] ?? []);
            $counts['media'] += $attached;
            $counts['media_skipped'] += $skipped;
        }
    }

    /**
     * Pass 2: now that every element has a real new id, resolve parent_id
     * using the legacy_id -> new_id map built during pass 1.
     */
    private function linkElementParents(Tenant $tenant, bool $dryRun): void
    {
        if ($dryRun) {
            return;
        }

        foreach ($this->elementLegacyIdToParentLegacyId as $legacyId => $legacyParentId) {
            $newId = $this->elementLegacyIdToNewId[$legacyId] ?? null;
            $newParentId = $this->elementLegacyIdToNewId[$legacyParentId] ?? null;

            if (! $newId) {
                continue;
            }

            if (! $newParentId) {
                $this->warn("  [warn] element legacy_id={$legacyId}: could not resolve parent legacy_id={$legacyParentId} to a new id, leaving parent_id null");

                continue;
            }

            Element::withoutGlobalScope('tenant')->where('id', $newId)->update(['parent_id' => $newParentId]);
        }
    }

    private function importPages(array $items, Tenant $tenant, bool $dryRun, array &$counts): void
    {
        foreach ($items as $item) {
            $counts['pages']++;
            $this->line(" - page legacy_id={$item['legacy_id']} slug={$item['slug']}");

            if ($dryRun) {
                continue;
            }

            // No metadata column on pages in this example schema -- match on (tenant_id, slug) instead of legacy_id.
            $existing = Page::withoutGlobalScope('tenant')
                ->where('tenant_id', $tenant->id)
                ->where('slug', $item['slug'])
                ->first();

            $type = PageType::tryFrom($item['type'] ?? '') ?? PageType::HTML;

            if ($type->value !== ($item['type'] ?? null)) {
                $this->warn("  [warn] page legacy_id={$item['legacy_id']}: unrecognized legacy type \"{$item['type']}\", defaulting to \"{$type->value}\" -- confirm against real legacy data");
            }

            $attrs = [
                'tenant_id' => $tenant->id,
                'name' => $item['name'],
                'slug' => $item['slug'],
                'type' => $type,
                'is_active' => (bool) $item['is_active'],
                'content' => $this->decodeJsonField($item['content'] ?? null),
            ];

            $page = $existing ?: new Page;
            $page->fill($attrs);
            $page->tenant_id = $tenant->id;
            $page->save();

            [$attached, $skipped] = $this->attachMedia($page, $item['media'] ?? []);
            $counts['media'] += $attached;
            $counts['media_skipped'] += $skipped;
        }
    }

    private function importPosts(array $items, Tenant $tenant, string $tenantSlug, bool $dryRun, array &$counts): void
    {
        foreach ($items as $item) {
            $counts['posts']++;
            $this->line(" - post legacy_id={$item['legacy_id']} slug={$item['slug']}");

            if ($dryRun) {
                continue;
            }

            $existing = Post::withoutGlobalScope('tenant')
                ->where('tenant_id', $tenant->id)
                ->where('metadata->legacy_id', $item['legacy_id'])
                ->where('metadata->legacy_site', $tenantSlug)
                ->first();

            $metadata = $this->decodeJsonField($item['metadata'] ?? null);
            $metadata['legacy_id'] = $item['legacy_id'];
            $metadata['legacy_site'] = $tenantSlug;

            $status = $this->mapPostStatus($item['status'] ?? null);

            $attrs = [
                'tenant_id' => $tenant->id,
                'title' => $item['title'],
                'slug' => $item['slug'],
                'content' => $this->decodeJsonField($item['content'] ?? null),
                'metadata' => $metadata,
                'status' => $status,
                'publish_on' => $item['publish_on'] ?? null,
                'publish_date' => $item['publish_date'] ?? null,
            ];

            $post = $existing ?: new Post;
            $post->fill($attrs);
            $post->tenant_id = $tenant->id;
            $post->save();

            [$attached, $skipped] = $this->attachMedia($post, $item['media'] ?? []);
            $counts['media'] += $attached;
            $counts['media_skipped'] += $skipped;
        }
    }

    /**
     * NOT YET CONFIRMED against your own legacy data. Defaults
     * defensively to DRAFT rather than risk auto-publishing something
     * that shouldn't be live -- adjust the recognized values below to
     * match what your legacy `posts.status` column actually contains.
     */
    private function mapPostStatus(mixed $legacyStatus): PostStatus
    {
        $normalized = is_string($legacyStatus) ? strtolower(trim($legacyStatus)) : $legacyStatus;

        return match (true) {
            in_array($normalized, ['1', 1, true, 'active', 'published', 'publish'], true) => PostStatus::ACTIVE,
            default => PostStatus::DRAFT,
        };
    }

    /**
     * extract_and_upload.py's JSON hands back MySQL JSON columns as
     * already-JSON-ENCODED STRINGS (PyMySQL doesn't auto-decode them).
     * Decode here so an Eloquent `array` cast doesn't double-encode.
     */
    private function decodeJsonField(mixed $value): array
    {
        if (is_array($value)) {
            return $value;
        }

        if (is_string($value) && $value !== '') {
            $decoded = json_decode($value, true);

            if (json_last_error() === JSON_ERROR_NONE && is_array($decoded)) {
                return $decoded;
            }
        }

        return [];
    }

    /**
     * Attach each already-uploaded media entry to the model via your
     * media library package, downloading from the URL
     * extract_and_upload.py wrote and re-storing it through the new
     * platform's own configured disk.
     *
     * Returns [attachedCount, skippedCount].
     */
    private function attachMedia($model, array $mediaEntries): array
    {
        $attached = 0;
        $skipped = 0;

        foreach ($mediaEntries as $m) {
            if (empty($m['new_url'])) {
                // extract_and_upload.py already logged why (file not found locally, etc.)
                $skipped++;

                continue;
            }

            try {
                $model->addMediaFromUrl($m['new_url'])
                    ->usingFileName($m['file_name'])
                    ->toMediaCollection($m['collection_name'] ?: 'default');
                $attached++;
            } catch (\Throwable $e) {
                $this->warn("  [warn] failed to attach media {$m['file_name']}: ".$e->getMessage());
                $skipped++;
            }
        }

        return [$attached, $skipped];
    }
}
