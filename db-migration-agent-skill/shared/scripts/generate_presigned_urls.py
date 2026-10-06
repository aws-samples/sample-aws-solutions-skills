#!/usr/bin/env python3
"""Generates the ONE link handed to the customer for a soak window, per
execution-runbooks.md §Soak automation / dashboard.md §Presigned-URL viewing.

Run this at soak start, and renew before expiry, after dashboard/index.html + assets/ + the initial
status.json/activity-log.jsonl have been uploaded to the dashboard S3 bucket (see
cdk-stacks.md §soak-stack.ts for the bucket + upload step). It does two things:

1. Presigns a GET URL for every file the page needs (index.html, both assets, status.json,
   activity-log.jsonl), each valid for the same duration. Use 129600s (1.5 days) or
   302400s (3.5 days) to cover the 1/3-day tier plus an exit buffer. A 7-day tier needs
   648000s (7.5 days) of coverage, but S3 SigV4 caps EACH signature at 604800s: issue
   604800s, renew by day 6, and have the customer reopen the NEW link. Rewriting the
   bucket's index does not refresh URLs already embedded in an open browser page.
2. Rewrites index.html so its CSS href / JS src / data-source globals point at those
   presigned URLs instead of relative paths, then re-uploads that rewritten copy — and
   THIS is the part that would otherwise be a subtle, easy-to-miss bug: a relative fetch
   like `fetch('status.json')` made from a page loaded via a presigned URL resolves to
   `.../status.json` with NO query string at all (relative URLs never inherit the base
   document's query string) — against a private bucket, that's an unsigned request, i.e.
   an unconditional 403, not a stale-but-working fetch. Every sub-resource the page loads
   must carry its OWN presigned query string, embedded absolute, not left relative.

⚠️ CREDENTIAL-LONGEVITY CAVEAT (real AWS behavior, confirmed live twice) — a presigned URL
can never outlive the credentials used to SIGN it, no matter what Expires value you pass.
AWS: with temporary credentials "the URL expires when the credential expires ... even if
the URL was created with a later expiration time"; for an IAM role it "expires when the
role session expires" (S3 User Guide, "Sharing objects with presigned URLs"). Temporary
credentials — an assumed role (often a 1-hour max session), an EC2/Lambda execution role
(instance-profile credentials rotate every few hours), an SSO session — produce a URL whose
querystring claims days of validity but which stops working when THAT session ends, with a
confusing ExpiredToken/AccessDenied rather than a clean "expired". This script therefore
detects temporary credentials (a session token is present), prints the EFFECTIVE expiry =
min(requested, credential expiry) and a warning, and reports that effective time — never
the requested one — as the link's lifetime.

DEFAULT: re-issue on demand. Re-running this script is always safe (it starts from the
clean template each time — see below); when the customer needs to look, re-run it and send
the new CUSTOMER LINK, stating its effective expiry. Do NOT casually suggest creating
long-term IAM user keys to get a longer link.

ONLY IF a longer-lived link is genuinely required (e.g. a stakeholder who must watch a
multi-day soak without asking): propose a DEDICATED signing IAM user as a new long-term
credential, behind its own explicit A3 block in authorizations.md (it is new IAM
infrastructure), never silently:
  1. Scope it to `s3:GetObject` AND `s3:PutObject` on this bucket only (no console access,
     no other bucket). Both are required: the script presigns GETs AND re-uploads
     (`PutObject`) the rewritten `index.html` (`materialize_index_html`). With a
     customer-managed KMS key on the bucket, also `kms:Decrypt` (every customer GET is
     authorized as the signer) and `kms:GenerateDataKey` (the script's own put_object) on
     that key ARN. Optionally `s3:ListBucket` (head_bucket 200 instead of 403 — the region
     is resolved either way, see `resolve_bucket_region`).
  2. Run this script authenticated AS that user via the standard credential chain (e.g.
     `export AWS_PROFILE=<signing-user-profile>`; there is no `--profile` flag). Never put
     the key in argv or a generated file.
  3. Keep the key active for the window (the permission AND the key's existence are checked
     live on every GET), and deactivate/delete it at soak-exit as part of the same cleanup.
Even then a single SigV4 signature is capped at 604800 s (7 days).

Usage:
    python3 generate_presigned_urls.py --bucket my-dashboard-bucket --region ap-northeast-2 \
        --expires-seconds 604800

REGION (real failure, ap-northeast-2 engagement): a SigV4 presigned URL embeds the region
in its credential scope. A client built without region_name signs for the caller's default
region (often us-east-1), and a bucket elsewhere then rejects every customer GET with
`AuthorizationQueryParametersError ... the region 'us-east-1' is wrong; expecting
'ap-northeast-2'` — while this script's own put_object still SUCCEEDS (boto3 follows the
region redirect for API calls, not for URLs it hands out), so the run looked green. This
script therefore (1) resolves the bucket's real region from S3 itself (head_bucket's
x-amz-bucket-region header, falling back to get_bucket_location), (2) signs with a client
pinned to that region on the regional virtual-hosted endpoint, (3) treats `--region` only
as an assertion that must match the detected region, and (4) GETs every URL it generated
and refuses to print the customer link unless each one returns HTTP 200 — on failure it
prints a redacted S3 error summary (Code/Message/Region only — never signing material) and exits
non-zero. Run it only after every object (index.html,
assets/, status.json, activity-log.jsonl) is already in the bucket.

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
import datetime
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ASSET_KEYS = ["assets/dashboard.css", "assets/dashboard.js"]
DATA_KEYS = ["status.json", "activity-log.jsonl"]
DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
# Presence of this string means a file is an already-materialized output of this script,
# not the clean template — used to fail loudly instead of silently double-injecting.
_MATERIALIZED_MARKER = "DASHBOARD_STATUS_URL"


def resolve_bucket_region(bucket, probe_client=None, hint_region=None):
    """Return the bucket's real region, asked of S3 itself — never the caller's default.

    head_bucket carries `x-amz-bucket-region` on 200, 301 AND 403 responses, so a signer
    scoped to Get/PutObject only (no s3:ListBucket) still learns the region from the error
    response. Fallback: get_bucket_location (LocationConstraint None/"" means us-east-1;
    legacy "EU" means eu-west-1)."""
    s3 = probe_client or boto3.client("s3", region_name=hint_region,
                                      config=Config(signature_version="s3v4"))
    try:
        resp = s3.head_bucket(Bucket=bucket)
        headers = resp.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    except ClientError as e:
        headers = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
            raise SystemExit(f"Bucket {bucket!r} does not exist (head_bucket 404).")
    region = headers.get("x-amz-bucket-region")
    if region:
        return region
    try:
        loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    except ClientError as e:
        raise SystemExit(
            f"Could not determine the region of bucket {bucket!r}: head_bucket returned no "
            f"x-amz-bucket-region header and get_bucket_location failed "
            f"({e.response.get('Error', {}).get('Code')}: {e}). Grant s3:ListBucket or "
            "s3:GetBucketLocation on the bucket to the signer, then re-run."
        )
    if not loc:
        return "us-east-1"
    return {"EU": "eu-west-1"}.get(loc, loc)


def credential_lifetime(session=None, environ=None):
    """(is_temporary, expiry_utc_or_None) for the credentials this script signs with.
    Temporary = a session token is present (STS/role/SSO/instance profile/container).
    The expiry is reported ONLY from an authoritative source: botocore's refreshable
    credentials (assumed role, instance/container metadata, SSO, credential_process carry
    `_expiry_time`), or the AWS_CREDENTIAL_EXPIRATION environment variable that credential
    exporters set next to AWS_SESSION_TOKEN. Otherwise it is unknown (None) — never guessed."""
    import os
    environ = os.environ if environ is None else environ
    creds = (session or boto3.Session()).get_credentials()
    if creds is None:
        return False, None
    frozen = creds.get_frozen_credentials()
    temporary = bool(frozen.token)
    expiry = getattr(creds, "_expiry_time", None)
    if not isinstance(expiry, datetime.datetime) and temporary and environ.get("AWS_CREDENTIAL_EXPIRATION"):
        try:
            expiry = datetime.datetime.fromisoformat(environ["AWS_CREDENTIAL_EXPIRATION"].replace("Z", "+00:00"))
        except ValueError:
            expiry = None
    if isinstance(expiry, datetime.datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=datetime.timezone.utc)
    return temporary, (expiry if isinstance(expiry, datetime.datetime) else None)


def effective_expiry(requested_seconds, temporary, cred_expiry, now=None):
    """(effective_expiry_utc_or_None, effective_seconds_or_None, warning_or_None).
    None = unknown: temporary credentials whose expiry is not authoritatively known — the
    caller must say "unknown", never print a precise time."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    wanted = now + datetime.timedelta(seconds=requested_seconds)
    if not temporary:
        return wanted, requested_seconds, None
    if cred_expiry is None:
        return None, None, (
            "WARNING: signed with TEMPORARY credentials (session token present) whose expiry is "
            "not known — these links stop working when that session ends (an assumed role is "
            "often 1 hour), NOT after the requested time. Default: re-issue on demand (re-run "
            "this script). A longer-lived link needs a dedicated signing user behind an explicit "
            "A3 approval — see this script's docstring; never suggest long-term keys casually.")
    eff = min(wanted, cred_expiry)
    secs = max(0, int((eff - now).total_seconds()))
    if eff < wanted:
        return eff, secs, (
            f"WARNING: signed with TEMPORARY credentials expiring {cred_expiry.isoformat()} — these "
            f"links stop working then (in {secs // 3600}h {secs % 3600 // 60}m), NOT after the requested "
            f"{requested_seconds}s. Default: re-issue on demand (re-run this script and send the new "
            "link). A longer-lived link needs a dedicated signing user behind an explicit A3 "
            "approval — see this script's docstring; never suggest long-term keys casually.")
    return eff, secs, None


def expiry_line(eff_at, requested_seconds, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if eff_at is None:
        at_most = (now + datetime.timedelta(seconds=requested_seconds)).isoformat(timespec="seconds")
        return ("EFFECTIVE EXPIRY: unknown — no later than the credential session's expiry "
                f"(temporary credentials), at most {at_most}")
    return f"EFFECTIVE EXPIRY: {eff_at.isoformat(timespec='seconds')}"


def make_signing_client(region, bucket, session=None):
    """S3 client pinned to the bucket's region, SigV4, virtual-hosted addressing (path-style
    only when the bucket name contains dots, which break virtual-host TLS). botocore
    resolves the regional endpoint itself (incl. non-aws partitions); us-east-1 is forced
    onto its regional endpoint instead of the legacy global one."""
    addressing = "path" if "." in bucket else "virtual"
    return (session or boto3).client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": addressing, "us_east_1_regional_endpoint": "regional"}),
    )


# Never echo signing material: S3 error bodies (SignatureDoesNotMatch etc.) can carry the
# StringToSign / CanonicalRequest / credential scope / security token.
_SAFE_ERROR_FIELDS = ("Code", "Message", "Region", "BucketRegion", "Endpoint")
_REDACT = re.compile(
    r"(X-Amz-(?:Credential|Security-Token|Signature)=)[^&\s<\"']+|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b", re.IGNORECASE)


def summarize_s3_error(status, body):
    """HTTP status + whitelisted, redacted fields from an S3 XML error body."""
    parts = [f"HTTP {status}"]
    for field in _SAFE_ERROR_FIELDS:
        m = re.search(rf"<{field}>(.*?)</{field}>", body or "", re.S)
        if m:
            val = _REDACT.sub(lambda mm: (mm.group(1) or "") + "<redacted>", m.group(1).strip())
            parts.append(f"{field}={val[:300]}")
    if len(parts) == 1 and body:
        parts.append("(non-XML error body suppressed)")
    return " ".join(parts)


def verify_url(url, timeout=20):
    """GET the presigned URL exactly as the customer's browser would. Returns
    (ok, status, body_excerpt)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(1)
            return r.status == 200, r.status, ""
    except urllib.error.HTTPError as e:
        body = e.read(4000).decode("utf-8", "replace")
        return False, e.code, summarize_s3_error(e.code, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, None, f"{type(e).__name__}: {getattr(e, 'reason', '')}"


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
    ap.add_argument("--region", default=None,
                     help="the dashboard bucket's region (e.g. ap-northeast-2). Optional — the "
                          "script always detects the bucket's real region from S3; if given, "
                          "it must MATCH the detected one or the script stops.")
    ap.add_argument("--expires-seconds", type=int, default=604800,
                     help="max 604800 (7 days) — the SigV4 ceiling; temporary signing credentials cap it further "
                          "(the script prints the effective expiry)")
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
    #
    # Pin the region too: SigV4 signs a region into every URL, so a client left on the
    # caller's default region produces URLs a bucket in another region rejects (see the
    # REGION note in the module docstring).
    region = resolve_bucket_region(args.bucket, hint_region=args.region)
    if args.region and args.region != region:
        sys.exit(f"--region {args.region} does not match the bucket's actual region {region} "
                 f"(reported by S3 for {args.bucket!r}). Fix the argument/config — URLs "
                 "signed for the wrong region are rejected by S3.")
    session = boto3.Session()
    temporary, cred_expiry = credential_lifetime(session)
    eff_at, eff_seconds, cred_warning = effective_expiry(args.expires_seconds, temporary, cred_expiry)
    if cred_warning:
        print(cred_warning, file=sys.stderr)
    s3 = make_signing_client(region, args.bucket, session)
    all_keys = ["index.html"] + ASSET_KEYS + DATA_KEYS
    presigned = {k: presign(s3, args.bucket, f"{args.prefix}{k}", args.expires_seconds) for k in all_keys}

    with open(args.local_template, encoding="utf-8") as f:
        template = f.read()

    materialized = materialize_index_html(template, presigned)
    s3.put_object(Bucket=args.bucket, Key=f"{args.prefix}index.html",
                  Body=materialized.encode("utf-8"), ContentType="text/html")

    # Self-verify: a successful put_object proves nothing about the URLs (boto3 follows
    # region redirects for its own calls). GET each URL like the customer's browser will.
    failures = []
    for k in all_keys:
        ok, status, body = verify_url(presigned[k])
        print(f"verify GET {args.prefix}{k}: {'200 OK' if ok else f'FAILED (HTTP {status})'}")
        if not ok:
            failures.append((k, status, body))
    if failures:
        for k, status, body in failures:
            print(f"--- S3 error for {args.prefix}{k}: {body}", file=sys.stderr)
        sys.exit(f"{len(failures)} of {len(all_keys)} presigned URLs did not return HTTP 200 — "
                 "NOT handing out a link. Common causes: object not uploaded yet (404 NoSuchKey), "
                 "signer lacks s3:GetObject or kms:Decrypt on a CMK bucket (403 AccessDenied), "
                 "region mismatch (400 AuthorizationQueryParametersError).")

    if eff_at is None:
        lifetime = "EFFECTIVE expiry unknown (temporary signing credentials without expiry metadata)"
    else:
        lifetime = (f"EFFECTIVE expiry {eff_at.isoformat(timespec='seconds')} ({eff_seconds / 3600:.1f} h)"
                    + (" — limited by temporary signing credentials" if eff_seconds < args.expires_seconds else ""))
    print(f"Bucket region: {region}. Presigned {len(all_keys)} objects (all verified HTTP 200). "
          f"Requested {args.expires_seconds}s; {lifetime}.")
    print("Re-uploaded index.html with absolute presigned references (css/js/status/log).")
    if temporary:
        print("Signed with temporary credentials: tell the customer the EFFECTIVE expiry above and "
              "re-issue on demand (re-run this script).")
    if cred_warning:
        print(cred_warning)
    print(expiry_line(eff_at, args.expires_seconds))
    print(f"CUSTOMER LINK: {presigned['index.html']}")


if __name__ == "__main__":
    main()
