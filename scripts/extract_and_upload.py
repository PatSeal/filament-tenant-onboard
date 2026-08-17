#!/usr/bin/env python3
"""
extract_and_upload.py

Step 1 of the tenant-onboarding migration pipeline.

Reads one legacy site's content (a generic "elements" flexible-content
table, plus "pages" and "posts") and its associated media (a polymorphic
"media" table, e.g. Spatie Laravel Media Library's convention) directly
from the legacy app's own database, uploads each media file to a staging
prefix in S3-compatible storage, and writes everything out as a single
JSON file for Step 2 (the Artisan import command) to consume.

WHY THIS READS .env FILES DIRECTLY instead of taking credentials as typed
flags: if this script runs on the same server that hosts both the legacy
app and the new platform, it can read each app's own already-configured
.env file (view-only — this script never writes to or modifies either
one) instead of requiring you to copy/paste real credentials by hand.
If you don't have shell access to a shared server, pass --db-host /
--db-name / --db-user / --db-password / --s3-* flags explicitly instead
(see --help).

SAFETY GUARDS BUILT IN:

1. Real (non-dry-run) uploads require an explicit --live flag. Without
   it, this script ALWAYS behaves like --dry-run, no matter what.

2. Refuses to write to any --s3-prefix starting with "production" or
   "live" -- the conventional names for a prefix that might already
   hold real, live production data -- unless you also pass
   --allow-production-prefix. Use a separate staging prefix instead,
   e.g. "migration-staging/<tenant-slug>". This is a speed bump, not a
   real security boundary -- don't rely on it as your only safeguard.

3. This script NEVER writes to, edits, or modifies either .env file it
   reads from. It only opens them for reading, in memory, to pull out
   the handful of variables it needs.

USAGE -- dry run:

    python3 extract_and_upload.py \
        --site <tenant-slug> \
        --legacy-root /var/www/<legacy-site-folder> \
        --platform-root /var/www/<new-platform-folder> \
        --s3-prefix migration-staging/<tenant-slug> \
        --output /tmp/migration_<tenant-slug>.json \
        --dry-run

Then, once the dry-run output JSON looks right, drop --dry-run and add
--live to actually upload for real:

    python3 extract_and_upload.py ... --live

If a legacy site's real media storage path isn't the default
{legacy-root}/storage/app/public (e.g. because of a releases/shared
deployment layout, or a symlink pointing elsewhere), override it
explicitly:

    python3 extract_and_upload.py ... --images-root /var/www/<legacy-site-folder>/shared/storage/app/public
"""

import argparse
import json
import os
import sys
import mimetypes
from pathlib import Path

try:
    import pymysql
except ImportError:
    print("Missing dependency: pip install pymysql --break-system-packages")
    sys.exit(1)

try:
    import boto3
    from botocore.config import Config
except ImportError:
    print("Missing dependency: pip install boto3 --break-system-packages")
    sys.exit(1)


# Adjust these to match your own legacy schema's model class names, as
# stored in the media table's polymorphic "model_type" column.
MODEL_TYPE_BY_TABLE = {
    "elements": "App\\Models\\Element",
    "pages": "App\\Models\\Page",
    "posts": "App\\Models\\Post",
}

# Prefixes treated as "probably already holds real production data" and
# refused by default -- see --allow-production-prefix. Adjust to match
# your own naming convention.
UNSAFE_PREFIXES = ("production", "live")


def parse_env_file(path):
    """
    Read-only .env parser. Opens the file, reads KEY=VALUE lines into
    a dict, and returns it. Never writes to or modifies the file in
    any way -- this function has no write/open('w') path at all.
    """
    if not os.path.isfile(path):
        print(f"Could not find .env at {path} -- check --legacy-root / --platform-root.")
        sys.exit(1)

    values = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip matching surrounding quotes, if present.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    return values


def parse_args():
    p = argparse.ArgumentParser(
        description="Extract a legacy site's content + media and upload images to S3-compatible storage -- "
                    "reads real credentials from each app's own .env file when run on a shared server."
    )
    p.add_argument("--site", required=True, help="Tenant slug for the site being migrated, e.g. acme, north-branch")
    p.add_argument("--legacy-root", required=False,
                    help="Path to the legacy app's own folder on the server, e.g. /var/www/legacy-acme. "
                         "Its .env file is READ (never modified) to get DB credentials. Omit and use "
                         "--db-host/--db-name/--db-user/--db-password instead if not running on a shared server.")
    p.add_argument("--platform-root", required=False,
                    help="Path to the new platform's own deployed folder on the server, e.g. /var/www/platform. "
                         "Its .env file is READ (never modified) to get storage credentials. Omit and use "
                         "--s3-* flags instead if not running on a shared server.")
    p.add_argument("--images-root", default=None,
                    help="Override the local images path if it's not the default {legacy-root}/storage/app/public "
                         "(e.g. if the deployment uses a releases/shared layout -- point this at the real target "
                         "of the storage/app/public symlink).")
    p.add_argument("--db-host", default=None, help="Legacy DB host, if not reading from --legacy-root's .env")
    p.add_argument("--db-port", default="3306", help="Legacy DB port (default 3306)")
    p.add_argument("--db-name", default=None, help="Legacy DB name, if not reading from --legacy-root's .env")
    p.add_argument("--db-user", default=None, help="Legacy DB user, if not reading from --legacy-root's .env")
    p.add_argument("--db-password", default=None, help="Legacy DB password, if not reading from --legacy-root's .env")
    p.add_argument("--s3-endpoint-url", default=None, help="S3-compatible API endpoint, if not reading from --platform-root's .env")
    p.add_argument("--s3-bucket", default=None, help="Bucket name, if not reading from --platform-root's .env")
    p.add_argument("--s3-public-url", default=None, help="Public base URL for building browser-loadable links, if not reading from --platform-root's .env")
    p.add_argument("--s3-region", default="us-east-1", help="Storage region (default us-east-1)")
    p.add_argument("--s3-prefix", required=True,
                    help="Key prefix in the bucket, e.g. migration-staging/<tenant-slug>. Refused if it starts "
                         "with 'production' or 'live' unless --allow-production-prefix is also passed.")
    p.add_argument("--output", required=True, help="Path to write the resulting JSON file")
    p.add_argument("--dry-run", action="store_true",
                    help="Do everything except actually upload. This is ALSO the default even without this flag -- "
                         "see --live.")
    p.add_argument("--live", action="store_true",
                    help="REQUIRED to actually upload for real. Without this flag, the script always dry-runs, "
                         "regardless of whether --dry-run was passed.")
    p.add_argument("--allow-production-prefix", action="store_true",
                    help="Explicitly allow --s3-prefix to start with 'production' or 'live'. Do not pass this "
                         "unless you specifically mean to write there.")
    return p.parse_args()


def connect_db(db_env):
    return pymysql.connect(
        host=db_env.get("DB_HOST", "127.0.0.1"),
        port=int(db_env.get("DB_PORT", "3306")),
        user=db_env["DB_USERNAME"],
        password=db_env["DB_PASSWORD"],
        database=db_env["DB_DATABASE"],
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_media_for(conn, model_type, model_id):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, collection_name, name, file_name, mime_type, disk, size, order_column
            FROM media
            WHERE model_type = %s AND model_id = %s
            ORDER BY order_column, id
        """, (model_type, model_id))
        return cur.fetchall()


def fetch_elements(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, parent_id, `order`, `key`, slug, content, is_active, metadata
            FROM elements
            ORDER BY id
        """)
        return cur.fetchall()


def fetch_pages(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, slug, type, is_active, content
            FROM pages
            ORDER BY id
        """)
        return cur.fetchall()


def fetch_posts(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, slug, content, metadata, status, user_id, publish_on, publish_date
            FROM posts
            ORDER BY id
        """)
        return cur.fetchall()


def upload_image(s3_client, bucket, local_path, s3_prefix, really_upload, public_url_base, url_includes_bucket):
    if not local_path or not os.path.isfile(local_path):
        print(f"  [warn] media file not found locally, skipping upload: {local_path}")
        return None

    filename = Path(local_path).name
    key = f"{s3_prefix}/{filename}"
    content_type, _ = mimetypes.guess_type(local_path)

    if not really_upload:
        print(f"[dry-run] would upload {local_path} -> s3://{bucket}/{key}")
    else:
        s3_client.upload_file(
            local_path,
            bucket,
            key,
            ExtraArgs={"ContentType": content_type or "application/octet-stream"},
        )
        print(f"  uploaded {local_path} -> s3://{bucket}/{key}")

    # Some S3-compatible providers configure a "public URL" setting that
    # ALREADY includes the bucket name in the path (unlike the raw API
    # endpoint, which usually doesn't). Naively appending "/{bucket}" in
    # both cases produces a doubled path (".../bucket/bucket/..."). Only
    # append the bucket name when the base URL doesn't already include it.
    if url_includes_bucket:
        return f"{public_url_base.rstrip('/')}/{key}"
    return f"{public_url_base.rstrip('/')}/{bucket}/{key}"


def build_media_entries(conn, s3_client, bucket, images_root, s3_prefix, model_type, model_id, really_upload, public_url_base, url_includes_bucket):
    entries = []
    for m in fetch_media_for(conn, model_type, model_id):
        # Default media-library disk convention: {disk_root}/{media.id}/{file_name}
        local_path = os.path.join(images_root, str(m["id"]), m["file_name"])
        new_url = upload_image(s3_client, bucket, local_path, s3_prefix, really_upload, public_url_base, url_includes_bucket)
        entries.append({
            "media_id": m["id"],
            "collection_name": m["collection_name"],
            "original_name": m["name"],
            "file_name": m["file_name"],
            "mime_type": m["mime_type"],
            "new_url": new_url,
        })
    return entries


def main():
    args = parse_args()

    if args.s3_prefix.startswith(UNSAFE_PREFIXES) and not args.allow_production_prefix:
        print(f"Refusing: --s3-prefix '{args.s3_prefix}' starts with a prefix name conventionally used for real "
              f"live data ({', '.join(UNSAFE_PREFIXES)}).")
        print(f"Use a separate staging prefix instead (e.g. migration-staging/{args.site}), or pass "
              f"--allow-production-prefix if you specifically mean to write here.")
        sys.exit(1)

    really_upload = args.live and not args.dry_run
    if not really_upload:
        reason = "--dry-run passed" if args.dry_run else "no --live flag passed"
        print(f"Running in DRY-RUN mode ({reason}). No files will actually be uploaded.")

    # --- Resolve legacy DB credentials: either read-only from the legacy app's own .env, or from flags ---
    if args.legacy_root:
        legacy_env_path = os.path.join(args.legacy_root, ".env")
        print(f"Reading (view-only) {legacy_env_path} for legacy DB credentials...")
        db_env = parse_env_file(legacy_env_path)
    else:
        if not (args.db_name and args.db_user and args.db_password):
            print("Provide either --legacy-root, or all of --db-name/--db-user/--db-password.")
            sys.exit(1)
        db_env = {
            "DB_HOST": args.db_host or "127.0.0.1",
            "DB_PORT": args.db_port,
            "DB_DATABASE": args.db_name,
            "DB_USERNAME": args.db_user,
            "DB_PASSWORD": args.db_password,
        }

    for required_key in ("DB_DATABASE", "DB_USERNAME", "DB_PASSWORD"):
        if required_key not in db_env:
            print(f"'{required_key}' not found/provided for legacy DB connection.")
            sys.exit(1)

    images_root = args.images_root or (
        os.path.join(args.legacy_root, "storage", "app", "public") if args.legacy_root else None
    )
    if not images_root:
        print("Provide --images-root when not using --legacy-root.")
        sys.exit(1)
    print(f"Using images root: {images_root}")
    if not os.path.isdir(images_root):
        print(f"[warn] {images_root} does not exist or isn't readable -- double check --images-root, or whether "
              f"this site uses a releases/shared deployment layout where storage/app/public is a symlink elsewhere.")

    # --- Resolve storage credentials: either read-only from the new platform's own .env, or from flags ---
    if args.platform_root:
        platform_env_path = os.path.join(args.platform_root, ".env")
        print(f"Reading (view-only) {platform_env_path} for storage credentials...")
        aws_env = parse_env_file(platform_env_path)
        bucket = aws_env.get("AWS_BUCKET")
        endpoint_url = aws_env.get("AWS_ENDPOINT")
        aws_url = aws_env.get("AWS_URL")
        region = aws_env.get("AWS_DEFAULT_REGION", "us-east-1")
        access_key = aws_env.get("AWS_ACCESS_KEY_ID", "")
        secret_key = aws_env.get("AWS_SECRET_ACCESS_KEY", "")
    else:
        bucket = args.s3_bucket
        endpoint_url = args.s3_endpoint_url
        aws_url = args.s3_public_url
        region = args.s3_region
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

    if not bucket:
        print("Could not resolve a bucket name -- provide --platform-root or --s3-bucket.")
        sys.exit(1)
    if not endpoint_url:
        print("Could not resolve a storage API endpoint -- provide --platform-root or --s3-endpoint-url.")
        sys.exit(1)

    if aws_url:
        # A public URL setting, when present, sometimes already includes the
        # bucket name in its path -- see the note in upload_image().
        public_url_base = aws_url
        url_includes_bucket = True
    else:
        public_url_base = endpoint_url
        url_includes_bucket = False

    print(f"Bucket: {bucket}   Endpoint: {endpoint_url}   Public URL base: {public_url_base}   Prefix: {args.s3_prefix}")

    os.environ["AWS_ACCESS_KEY_ID"] = access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
    os.environ["AWS_DEFAULT_REGION"] = region

    s3_config = Config(s3={"addressing_style": "path"})
    s3_client = boto3.client("s3", endpoint_url=endpoint_url, config=s3_config)

    conn = connect_db(db_env)
    results = {"elements": [], "pages": [], "posts": []}
    try:
        for row in fetch_elements(conn):
            media = build_media_entries(
                conn, s3_client, bucket, images_root, args.s3_prefix,
                MODEL_TYPE_BY_TABLE["elements"], row["id"], really_upload, public_url_base, url_includes_bucket,
            )
            results["elements"].append({
                "legacy_id": row["id"],
                "parent_id": row["parent_id"],
                "key": row["key"],
                "slug": row["slug"],
                "content": row["content"],
                "metadata": row["metadata"],
                "is_active": row["is_active"],
                "media": media,
            })

        for row in fetch_pages(conn):
            media = build_media_entries(
                conn, s3_client, bucket, images_root, args.s3_prefix,
                MODEL_TYPE_BY_TABLE["pages"], row["id"], really_upload, public_url_base, url_includes_bucket,
            )
            results["pages"].append({
                "legacy_id": row["id"],
                "name": row["name"],
                "slug": row["slug"],
                "type": row["type"],
                "content": row["content"],
                "is_active": row["is_active"],
                "media": media,
            })

        for row in fetch_posts(conn):
            media = build_media_entries(
                conn, s3_client, bucket, images_root, args.s3_prefix,
                MODEL_TYPE_BY_TABLE["posts"], row["id"], really_upload, public_url_base, url_includes_bucket,
            )
            results["posts"].append({
                "legacy_id": row["id"],
                "title": row["title"],
                "slug": row["slug"],
                "content": row["content"],
                "metadata": row["metadata"],
                "status": row["status"],
                "publish_on": str(row["publish_on"]) if row["publish_on"] else None,
                "publish_date": str(row["publish_date"]) if row["publish_date"] else None,
                "media": media,
            })
    finally:
        conn.close()

    with open(args.output, "w") as f:
        json.dump({"site": args.site, **results}, f, indent=2, default=str)

    total = len(results["elements"]) + len(results["pages"]) + len(results["posts"])
    print(f"Wrote {total} item(s) ({len(results['elements'])} elements, {len(results['pages'])} pages, "
          f"{len(results['posts'])} posts) to {args.output}")
    if not really_upload:
        print("This was a DRY RUN -- no images were actually uploaded.")


if __name__ == "__main__":
    main()
