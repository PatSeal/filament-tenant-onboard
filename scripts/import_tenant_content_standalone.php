<?php

/**
 * import_tenant_content_standalone.php
 *
 * Fallback for Step 2 when you can't (or don't want to) deploy
 * ImportTenantContent.php as a versioned Artisan command -- e.g. a
 * shared/locked-down deployment layout where you don't have write
 * access to the app's own app/Console/Commands/ folder, or you'd
 * rather run this from a completely separate location than the app
 * itself.
 *
 * WHAT THIS DOES: manually bootstraps the target Laravel application
 * (same as artisan.php / a queue worker would), applies a runtime
 * config override for your media library package's temporary staging
 * directory (see docs/SERVER_PERMISSIONS.md for exactly why this is
 * needed), then invokes the real `migrate:tenant` Artisan command
 * in-process via Artisan::call().
 *
 * WHY Artisan::call() HERE MATTERS: running `php artisan migrate:tenant
 * ...` fresh at a shell prompt starts a brand-new PHP process with no
 * knowledge of any config override you set elsewhere. But calling
 * Artisan::call('migrate:tenant', [...]) from *within* a script that
 * already bootstrapped the same framework instance runs the real
 * command logic in-process -- so a config() override applied earlier
 * in this same script actually takes effect for it. This is the exact
 * same trick that works inside an interactive `tinker` session too.
 *
 * USAGE:
 *
 *     php import_tenant_content_standalone.php /var/www/<platform-folder> /tmp/migration_<slug>.json --dry-run
 *     php import_tenant_content_standalone.php /var/www/<platform-folder> /tmp/migration_<slug>.json
 *
 * This script does not modify the target app's own files -- it only
 * requires its autoloader and bootstraps its framework in-memory for
 * the duration of this process, exactly like a normal `php artisan`
 * invocation would.
 */

if ($argc < 3) {
    fwrite(STDERR, "Usage: php import_tenant_content_standalone.php <platform-app-root> <json-path> [--dry-run]\n");
    exit(1);
}

$appRoot = rtrim($argv[1], '/');
$jsonPath = $argv[2];
$dryRun = in_array('--dry-run', $argv, true);

if (! is_file($appRoot.'/artisan')) {
    fwrite(STDERR, "'{$appRoot}' doesn't look like a Laravel app root (no artisan file found there).\n");
    exit(1);
}

if (! is_file($jsonPath)) {
    fwrite(STDERR, "JSON file not found: {$jsonPath}\n");
    exit(1);
}

require $appRoot.'/vendor/autoload.php';

/** @var \Illuminate\Foundation\Application $app */
$app = require $appRoot.'/bootstrap/app.php';
$app->make(\Illuminate\Contracts\Console\Kernel::class)->bootstrap();

// See docs/SERVER_PERMISSIONS.md -- this line is the fix for the media
// library temp-directory permission issue. Redirects the package's
// temp staging to somewhere already world-writable (/tmp), since
// Artisan::call() below runs in this same in-memory process and will
// pick up this override, unlike a fresh `php artisan ...` invocation.
config(['media-library.temporary_directory_path' => '/tmp/migration-media-temp']);

// ---- Tiny console-output helpers (stand-ins for $this->info() etc. when running outside a Command class) ----
function out(string $line): void
{
    fwrite(STDOUT, $line."\n");
}

out('Bootstrapped '.$appRoot.' -- running migrate:tenant '.($dryRun ? '[DRY RUN]' : '[LIVE]').' against '.$jsonPath);

$options = ['json_path' => $jsonPath];
if ($dryRun) {
    $options['--dry-run'] = true;
}

$exitCode = \Illuminate\Support\Facades\Artisan::call('migrate:tenant', $options);

out(\Illuminate\Support\Facades\Artisan::output());

exit($exitCode);
