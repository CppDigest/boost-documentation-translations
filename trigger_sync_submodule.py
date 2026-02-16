#!/usr/bin/env python3
"""
Trigger the sync-submodule workflow via repository_dispatch.

Submodule list is taken from .gitmodules on master (ORG/TRANSLATIONS_REPO).
Ref logic: if version is given, used for libs; if omitted, workflow uses master/develop.

Usage:
  export GITHUB_TOKEN=your_pat
  python trigger_sync_submodule.py

  python trigger_sync_submodule.py --version boost-1.90.0
  python trigger_sync_submodule.py --version boost-1.90.0 --lang-code zh_Hans --extensions .adoc .md
"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API_BASE = "https://api.github.com"
EVENT_TYPE = "sync-submodule"
DEFAULT_OWNER = "CppDigest"
DEFAULT_REPO = "boost-documentation-translations"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trigger sync-submodule workflow via repository_dispatch"
    )
    parser.add_argument(
        "--owner",
        default=os.environ.get("GITHUB_REPOSITORY_OWNER", DEFAULT_OWNER),
        help="Repository owner (default: CppDigest or GITHUB_REPOSITORY_OWNER)",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="Repository name (default: boost-documentation-translations)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub PAT (default: GITHUB_TOKEN env)",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Boost release ref (e.g. boost-1.90.0). If set, used for lib repos; "
             "if omitted, workflow uses master (boost) and develop (libs).",
    )
    parser.add_argument(
        "--lang-code",
        default=None,
        metavar="LANG",
        help="Language code for Weblate (e.g. zh_Hans). Optional; passed to sync_submodule.py.",
    )
    parser.add_argument(
        "--extensions",
        nargs="*",
        metavar="EXT",
        default=None,
        help="File extensions for Weblate (e.g. .adoc .md). Passed to workflow when WEBLATE_URL/WEBLATE_TOKEN are set.",
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: GITHUB_TOKEN or --token required", file=sys.stderr)
        sys.exit(1)

    payload = {"event_type": EVENT_TYPE}
    client_payload = {}
    if args.version is not None:
        client_payload["version"] = args.version
    if args.lang_code is not None:
        client_payload["lang_code"] = args.lang_code
    if args.extensions:
        client_payload["extensions"] = ",".join(
            "." + e.lstrip(".") for e in args.extensions
        )
    if client_payload:
        payload["client_payload"] = client_payload

    url = f"{API_BASE}/repos/{args.owner}/{args.repo}/dispatches"
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {args.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=30) as r:
            if r.status in (200, 204):
                print(f"Workflow triggered: {args.owner}/{args.repo} ({EVENT_TYPE})")
                if client_payload:
                    print("Payload:", json.dumps(client_payload, indent=2))
            else:
                print(f"Unexpected status: {r.status}", file=sys.stderr)
                sys.exit(1)
    except HTTPError as e:
        print(f"Request failed: {e.code} {e.reason}", file=sys.stderr)
        if e.fp:
            body = e.fp.read().decode("utf-8", errors="replace")
            print(body, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
