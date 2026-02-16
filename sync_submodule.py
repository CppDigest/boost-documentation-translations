#!/usr/bin/env python3
"""
Sync existing Boost libs submodules: read submodule list from .gitmodules of the
translations repo (ORG/TRANSLATIONS_REPO at MASTER_BRANCH), then run the same update
logic as add_submodule (clone, prune, sync CppDigest repos, update submodule pointers,
optionally trigger Weblate). No new submodules are added.

The .gitmodules used is the one in the current working directory. The workflow must
checkout MASTER_BRANCH before running this script so the list matches ORG/TRANSLATIONS_REPO.
Triggered by CI (repository_dispatch, event sync-submodule).
"""

import argparse
import os
import sys
import tempfile
from typing import List

from add_submodule import (
    ensure_translations_cloned,
    finalize_translations_repo,
    has_open_translation_pr,
    parse_extensions_list,
    parse_gitmodules,
    process_one_submodule,
    trigger_weblate_add_or_update,
)

GITMODULES_PATH = ".gitmodules"


def parse_gitmodules_libs() -> List[str]:
    """
    Read .gitmodules from cwd (must be ORG/TRANSLATIONS_REPO with MASTER_BRANCH checked out)
    and return list of lib submodule names (path without 'libs/' prefix). E.g. libs/algorithm -> 'algorithm'.
    """
    with open(GITMODULES_PATH, encoding="utf-8") as f:
        content = f.read()
    entries = parse_gitmodules(content)
    libs_prefix = "libs/"
    return [p[len(libs_prefix):] for _n, p in entries if p.startswith(libs_prefix)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync existing Boost submodules (submodule list from .gitmodules)"
    )
    parser.add_argument(
        "--gitmodules-ref",
        default="master",
        help="Ref for .gitmodules (unused; list comes from local .gitmodules)",
    )
    parser.add_argument(
        "--libs-ref",
        default="develop",
        help="Ref for library repos (e.g. boost-1.90.0 or develop)",
    )
    parser.add_argument("--org", default="CppDigest", help="Target organization")
    parser.add_argument(
        "--translations-repo",
        default="boost-documentation-translations",
        help="Repo holding submodule links",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token",
    )
    parser.add_argument(
        "--extensions",
        default="",
        metavar="LIST",
        help="List-like or JSON string for Weblate file extensions (e.g. [.adoc, .md])",
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: GITHUB_TOKEN or --token required", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(GITMODULES_PATH):
        print(
            f"Error: {GITMODULES_PATH} not found (run from translations repo with MASTER_BRANCH checked out)",
            file=sys.stderr,
        )
        sys.exit(1)

    submodule_names = parse_gitmodules_libs()
    if not submodule_names:
        print("No libs/ submodules in .gitmodules, nothing to sync.", file=sys.stderr)
        return

    lib_submodules = [(name, f"libs/{name}") for name in submodule_names]
    print(f"Syncing {len(lib_submodules)} submodules from {GITMODULES_PATH}.", file=sys.stderr)

    token = args.token
    org = args.org
    translations_repo = args.translations_repo
    libs_ref = args.libs_ref

    updates_master: List[str] = []
    updates_local: List[str] = []

    with tempfile.TemporaryDirectory() as work:
        boost_work = os.path.join(work, "boost")
        cppdigest_work = os.path.join(work, "cppdigest")
        translations_dir = os.path.join(work, "translations")
        os.makedirs(boost_work, exist_ok=True)
        os.makedirs(cppdigest_work, exist_ok=True)

        for i, (submodule_name, _path_in_boost) in enumerate(lib_submodules, 1):
            print(f"[{i}/{len(lib_submodules)}] {submodule_name} ...", file=sys.stderr)
            success = process_one_submodule(
                submodule_name,
                libs_ref,
                org,
                boost_work,
                cppdigest_work,
                token,
            )
            if not success:
                continue
            ensure_translations_cloned(org, translations_repo, translations_dir)
            updates_master.append(submodule_name)
            updates_local.append(submodule_name)

        if os.path.isdir(os.path.join(translations_dir, ".git")):
            finalize_translations_repo(
                translations_dir,
                libs_ref,
                token,
                updates_master,
                updates_local,
                org,
            )

    weblate_url = os.environ.get("WEBLATE_URL")
    weblate_token = os.environ.get("WEBLATE_TOKEN")
    if weblate_url and weblate_token and updates_master:
        any_open_pr = any(
            has_open_translation_pr(org, sub, libs_ref, token)
            for sub in updates_master
        )
        if not any_open_pr:
            extensions_list = parse_extensions_list(args.extensions)
            trigger_weblate_add_or_update(
                weblate_url,
                weblate_token,
                org,
                updates_master,
                None,
                libs_ref,
                extensions_list,
            )

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
