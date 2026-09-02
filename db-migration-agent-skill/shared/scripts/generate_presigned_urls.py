#!/usr/bin/env python3
"""Generates the ONE link handed to the customer for a soak window, per
execution-runbooks.md §Soak automation / dashboard.md §Presigned-URL viewing.

Run this ONCE, at soak start, after dashboard/index.html + assets/ + the initial
status.json/activity-log.jsonl have been uploaded to the dashboard S3 bucket (see
cdk-stacks.md §Soak automation infra for the bucket + upload step). It does two things:

1. Presigns a GET URL for every file the page needs (index.html, both assets, status.json,
   activity-log.jsonl), each valid for the same duration — the soak length (1/3/7 days).
2. Rewrites index.html so its CSS href / JS src / data-source globals point at those
   presigned URLs instead of relative paths, then re-uploads that rewritten copy — and
   THIS is the part that would otherwise be a subtle, easy-to-miss bug: a relative fetch
   like `fetch('status.json')` made from a page loaded via a presigned URL resolves to
   `.../status.json` with NO query string at all (relative URLs never inherit the base
   document's query string) — against a private bucket, that's an unsigned request, i.e.
   an unconditional 403, not a stale-but-working fetch. Every sub-resource the page loads
   must carry its OWN presigned query string, embedded absolute, not left relative.

⚠️ CREDENTIAL-LONGEVITY CAVEAT (real AWS behavior, confirmed while building this) — a
presigned URL can never outlive the credentials used to SIGN it, no matter what Expires
value you pass. Temporary/STS credentials (an assumed role, an EC2/Lambda execution role,
an SSO session) typically live 1-12 hours; asking for `--expires-seconds 604800` (7 days)
with those credentials produces a URL whose querystring claims 7-day validity but which
actually stops working the moment the underlying STS session expires — with a confusing
SignatureDoesNotMatch/ExpiredToken, not a clean "expired" message. The only credential type
that genuinely supports the full SigV4 604800s (7-day) ceiling is a long-term IAM user
access key. For any 3- or 7-day soak tier:
  1. Create a throwaway IAM user scoped to `s3:GetObject` on this bucket only, no console
     access, at soak start.
  2. Run this script authenticated AS that user (`--profile`/env vars pointing at its
     access key), not as your own role/SSO session.
  3. Keep the key alive for the entire soak window — the permission AND the key's
     existence are both checked live on every GET, not frozen at signing time.
  4. Deactivate/delete the key right after soak-exit (Phase 7.7 sign-off), as part of the
     same cleanup that ends the soak.
A 1-day soak can usually get away with an operator's own long-lived CLI credentials if
their session genuinely outlives 24h — check `aws sts get-caller-identity` /
`aws configure list` before relying on this; when in doubt, use the throwaway-IAM-user path
regardless of tier.

Usage:
    python3 generate_presigned_urls.py --bucket my-dashboard-bucket --expires-seconds 604800

By default the TEMPLATE is this repo's own clean `shared/templates/dashboard.html` — never
the bucket's current `index.html`. That default is deliberate, not just convenient: once
this script has run once, the bucket's copy is already materialized (absolute presigned
refs, injected globals), so re-reading it as if it were still a clean template and running
the same rewrite again would double-inject — confirmed live while building this, see
`materialize_index_html`'s docstring below. Re-running this script (e.g. because the first
set of URLs is about to expire, or the tier changed) is always safe because it starts from
the clean template again every time; only pass `--local-template` to point at a different
clean copy, never at something this script has already materialized.

Prints the customer-facing index.html URL last, on its own line, prefixed
"CUSTOMER LINK: " — that line is the deliverable to hand over.
"""
import argparse
import sys
from pathlib import Path

import boto3
from botocore.config import Config

ASSET_KEYS = ["assets/dashboard.css", "assets/dashboard.js"]
DATA_KEYS = ["status.json", "activity-log.jsonl"]
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
# Presence of this string means a file is an already-materialized output of this script,
# not the clean template — used to fail loudly instead of silently double-injecting.
_MATERIALIZED_MARKER = "DASHBOARD_STATUS_URL"


def presign(s3, bucket, key, expires_seconds):
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_seconds,
    )


def materialize_index_html(html_text, presigned):
    """Rewrite the template's relative references to absolute presigned URLs. Order
    matters: do the narrower `src=`/`href=` replacements before injecting the data-source
    globals, and inject the globals as their own <script> BEFORE the dashboard.js <script>
    tag, since dashboard.js reads window.DASHBOARD_STATUS_URL/DASHBOARD_LOG_URL at load
    time (see shared/assets/dashboard.js's fetchJSON/fetchJSONL).

    Must only ever be called with the CLEAN template — calling it twice on its own output
    (e.g. by re-downloading the bucket's already-materialized index.html and treating that
    as the template) does not error, it silently stacks a second, differently-signed set of
    <script> blocks on top of the first, because the injection point (`</body>`) is still
    there to match against even after the first injection. `main()` guards against this by
    always reading from `DEFAULT_TEMPLATE` unless told otherwise, never from the bucket."""
    if _MATERIALIZED_MARKER in html_text:
        raise ValueError(
            "This template is already a materialized output of this script (found "
            f"{_MATERIALIZED_MARKER!r}) — pass the clean shared/templates/dashboard.html "
            "instead of an already-rewritten copy, or the page ends up with duplicate, "
            "differently-expiring <script> blocks."
        )
    out = html_text
    out = out.replace('href="assets/dashboard.css"', f'href="{presigned["assets/dashboard.css"]}"')
    # Remove the ENTIRE original tag (not just its src=) — gutting only the attribute
    # leaves a dangling empty `<script ></script>` sitting in the markup.
    out = out.replace('<script src="assets/dashboard.js"></script>', "")
    config_script = (
        "<script>\n"
        f'  window.DASHBOARD_STATUS_URL = {presigned["status.json"]!r};\n'
        f'  window.DASHBOARD_LOG_URL = {presigned["activity-log.jsonl"]!r};\n'
        "</script>\n"
        f'<script src="{presigned["assets/dashboard.js"]}"></script>'
    )
    out = out.replace("</body>", f"{config_script}\n</body>")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", default="", help='key prefix inside the bucket, e.g. "" or "myeng/"')
    ap.add_argument("--expires-seconds", type=int, default=604800,
                     help="max 604800 (7 days) — the SigV4 ceiling; see credential-longevity caveat above")
    ap.add_argument("--local-template", default=str(DEFAULT_TEMPLATE),
                     help="path to the CLEAN index.html to rewrite (default: this repo's "
                          "own shared/templates/dashboard.html). Never point this at the "
                          "bucket's own current index.html once this script has run once — "
                          "see the module docstring for why.")
    args = ap.parse_args()

    if args.expires_seconds > 604800:
        sys.exit("--expires-seconds cannot exceed 604800 (7 days) — S3 SigV4's own ceiling.")

    # Force SigV4 explicitly. Confirmed live: boto3's default S3 client in us-east-1 (the
    # legacy global s3.amazonaws.com endpoint) still negotiates SigV2 (AWSAccessKeyId/
    # Signature/Expires query params) unless told otherwise — an easy silent surprise, since
    # every other region defaults to SigV4 already. SigV2 is a deprecated signing scheme;
    # forcing 's3v4' here is what makes the query-string-is-fully-signed behavior this
    # script (and dashboard.js's cache-buster removal) relies on actually hold.
    s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    all_keys = ["index.html"] + ASSET_KEYS + DATA_KEYS
    presigned = {k: presign(s3, args.bucket, f"{args.prefix}{k}", args.expires_seconds) for k in all_keys}

    with open(args.local_template, encoding="utf-8") as f:
        template = f.read()

    materialized = materialize_index_html(template, presigned)
    s3.put_object(Bucket=args.bucket, Key=f"{args.prefix}index.html",
                  Body=materialized.encode("utf-8"), ContentType="text/html")

    print(f"Presigned {len(all_keys)} objects, expiring in {args.expires_seconds}s "
          f"({args.expires_seconds / 86400:.1f} days).")
    print("Re-uploaded index.html with absolute presigned references (css/js/status/log).")
    print("Keep the signing credentials' underlying session alive for the full duration above")
    print("— see this script's own docstring for the long-term-IAM-user requirement on 3/7-day tiers.")
    print(f"CUSTOMER LINK: {presigned['index.html']}")


if __name__ == "__main__":
    main()
