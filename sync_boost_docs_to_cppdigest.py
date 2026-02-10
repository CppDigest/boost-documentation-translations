#!/usr/bin/env python3
"""
Sync Boost library documentation from boostorg/boost to CppDigest organization.

Triggered by CI (repository_dispatch). For each libs/ submodule:
1. Clone boostorg repo at given ref; keep only doc folders per meta/libraries.json.
2. Update or create CppDigest/<submodule> with that content on docs branch.
3. Update boost-documentation-translations submodule links in libs/ to point to
   latest commit of CppDigest/<submodule> master branch.

Reuses parsing and fetch logic from collect_boost_libraries_extensions.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Allow importing from data/ when run from repo root (e.g. CI)
_script_dir = os.path.dirname(os.path.abspath(__file__))
_data_dir = os.path.join(_script_dir, "data")
if os.path.isdir(_data_dir):
    sys.path.insert(0, _data_dir)

# Reuse from collect_boost_libraries_extensions
from collect_boost_libraries_extensions import (
    GITMODULES_URL_TEMPLATE,
    GITHUB_API_BASE,
    fetch_url,
    get_libraries_from_repo,
    parse_gitmodules,
)

USER_AGENT = "BoostDocsSync/1.0"
BOOST_ORG = "boostorg"
DOCS_BRANCH = "local"


def run(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run command; return CompletedProcess. Raises on non-zero if check=True."""
    env = (os.environ if env is None else {**os.environ, **env})
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def api_get(path: str, token: str) -> dict:
    """GET GitHub API path; return JSON. Raises on error."""
    url = f"{GITHUB_API_BASE}{path}"
    req = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT,
    })
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def api_get_status(path: str, token: str) -> int:
    """GET GitHub API path; return HTTP status code."""
    url = f"{GITHUB_API_BASE}{path}"
    req = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urlopen(req, timeout=30) as r:
            return r.status
    except HTTPError as e:
        return e.code


def repo_exists(org: str, repo: str, token: str) -> bool:
    """Return True if org/repo exists."""
    return api_get_status(f"/repos/{org}/{repo}", token) == 200


def create_repo(org: str, repo: str, token: str, private: bool = False) -> None:
    """Create repository in org. Fails if already exists."""
    url = f"{GITHUB_API_BASE}/orgs/{org}/repos"
    data = json.dumps({"name": repo, "private": private}).encode("utf-8")
    req = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=30) as _:
        pass


def doc_paths_to_keep(
    libs: List[Tuple[str, str, str]]
) -> Set[str]:
    """
    From get_libraries_from_repo result (first_col, repo_url, subpath),
    return set of directory paths to keep: "doc", "minmax/doc", "string/doc", etc.
    """
    out: Set[str] = set()
    for _, _, subpath in libs:
        path = "doc" if not subpath else f"{subpath}/doc"
        out.add(path)
    return out


def first_segments(paths: Set[str]) -> Set[str]:
    """For paths like doc, minmax/doc, string/doc return first segment set: doc, minmax, string."""
    segs: Set[str] = set()
    for p in paths:
        if not p:
            continue
        seg = p.split("/")[0]
        segs.add(seg)
    return segs


def paths_under(base: str, paths: Set[str]) -> Set[str]:
    """Return relative paths under base. E.g. base='minmax', paths={'doc','minmax/doc'} -> {'doc'}."""
    prefix = base + "/" if base else ""
    out: Set[str] = set()
    for p in paths:
        if p == base:
            out.add("")
            continue
        if p.startswith(prefix):
            rest = p[len(prefix):]
            out.add(rest.split("/")[0])
    return out


def prune_to_doc_only(clone_dir: str, paths_to_keep: Set[str]) -> None:
    """
    Remove everything in clone_dir except .git and directories/files under paths_to_keep.
    paths_to_keep is e.g. {"doc", "minmax/doc", "string/doc"}.
    """
    def prune_dir(base: str, keep_paths: Set[str]) -> None:
        current_segments = first_segments(keep_paths) if keep_paths else set()
        if not base:
            current_segments.discard("")
        full_base = os.path.join(clone_dir, base) if base else clone_dir
        if not os.path.isdir(full_base):
            return
        for name in os.listdir(full_base):
            if name == ".git" and not base:
                continue
            path = os.path.join(full_base, name)
            rel = f"{base}/{name}" if base else name
            if rel.startswith("/"):
                rel = rel[1:]
            if os.path.isfile(path):
                # Check if file is under any keep path
                under = any(
                    rel == p or rel.startswith(p + "/")
                    for p in paths_to_keep
                )
                if not under:
                    os.remove(path)
            else:
                if name not in current_segments and rel not in paths_to_keep:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    sub_paths = {p[len(rel):].lstrip("/") for p in keep_paths
                                 if p == rel or p.startswith(rel + "/")}
                    prune_dir(rel, sub_paths)

    prune_dir("", paths_to_keep)

    # Remove any top-level item that is not .git and not in first segments of paths_to_keep
    top_keep = first_segments(paths_to_keep)
    for name in list(os.listdir(clone_dir)):
        if name == ".git":
            continue
        path = os.path.join(clone_dir, name)
        if name not in top_keep and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif name not in top_keep and os.path.isfile(path):
            os.remove(path)


def clone_repo(
    repo_url: str,
    ref: str,
    dest: str,
    token: Optional[str] = None,
) -> None:
    """Clone repo at ref into dest. Uses token for HTTPS if provided."""
    os.makedirs(dest, exist_ok=True)
    url = authed_url(repo_url, token)
    run(["git", "clone", "--depth", "1", "--branch", ref, url, dest])
    # Remove .git so we can use dest as plain copy source if needed; caller may re-init
    shutil.rmtree(os.path.join(dest, ".git"), ignore_errors=True)


def authed_url(repo_url: str, token: Optional[str]) -> str:
    """Return repo_url with token embedded for HTTPS GitHub URLs."""
    if not token or "github.com" not in repo_url:
        return repo_url
    from urllib.parse import urlparse
    parsed = urlparse(repo_url)
    return f"{parsed.scheme}://x-access-token:{token}@{parsed.netloc}{parsed.path}"


def clone_repo_keep_git(
    repo_url: str,
    branch: str,
    dest: str,
    token: Optional[str] = None,
) -> None:
    """Clone repo (single branch) into dest, keeping .git."""
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    url = authed_url(repo_url, token)
    run(["git", "clone", "--depth", "1", "--branch", branch, url, dest])


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Boost docs to CppDigest")
    parser.add_argument("--gitmodules-ref", default="master", help="Ref for .gitmodules (e.g. master)")
    parser.add_argument("--libs-ref", default="develop", help="Ref for libs (e.g. boost-1.90.0 or develop)")
    parser.add_argument("--org", default="CppDigest", help="Target organization")
    parser.add_argument("--translations-repo", default="boost-documentation-translations", help="Repo holding submodule links")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N submodules (test)")
    args = parser.parse_args()

    if not args.token:
        print("Error: GITHUB_TOKEN or --token required", file=sys.stderr)
        sys.exit(1)

    token = args.token
    gitmodules_ref = args.gitmodules_ref
    libs_ref = args.libs_ref
    org = args.org
    translations_repo = args.translations_repo
    docs_branch = DOCS_BRANCH

    # 1) Fetch .gitmodules and list libs/ submodules
    url = GITMODULES_URL_TEMPLATE.format(ref=gitmodules_ref)
    print(f"Fetching .gitmodules from boostorg/boost at {gitmodules_ref}...", file=sys.stderr)
    try:
        gitmodules = fetch_url(url, token=token)
    except (HTTPError, URLError) as e:
        print(f"Failed to fetch .gitmodules: {e}", file=sys.stderr)
        sys.exit(1)

    submodules = parse_gitmodules(gitmodules)
    lib_submodules = [(n, p) for n, p in submodules if p.startswith("libs/")]
    if args.limit is not None:
        lib_submodules = lib_submodules[: args.limit]
        print(f"Limited to first {len(lib_submodules)} submodules.", file=sys.stderr)
    print(f"Found {len(lib_submodules)} libs submodules.", file=sys.stderr)

    with tempfile.TemporaryDirectory() as work:
        boost_work = os.path.join(work, "boost")
        cppdigest_work = os.path.join(work, "cppdigest")
        translations_dir = os.path.join(work, "translations")
        os.makedirs(boost_work, exist_ok=True)
        os.makedirs(cppdigest_work, exist_ok=True)

        for i, (submodule_name, _path_in_boost) in enumerate(lib_submodules, 1):
            print(f"[{i}/{len(lib_submodules)}] {submodule_name} ...", file=sys.stderr)

            # Libraries and doc paths for this submodule
            libs = get_libraries_from_repo(submodule_name, libs_ref)
            if not libs:
                print(f"  No libraries.json entries, skipping.", file=sys.stderr)
                continue
            paths_to_keep = doc_paths_to_keep(libs)
            if not paths_to_keep:
                print(f"  No doc paths, skipping.", file=sys.stderr)
                continue

            boost_repo_url = f"https://github.com/{BOOST_ORG}/{submodule_name}.git"
            submodule_clone = os.path.join(boost_work, submodule_name)
            try:
                clone_repo(boost_repo_url, libs_ref, submodule_clone, token=token)
            except subprocess.CalledProcessError as e:
                print(f"  Clone failed: {e.stderr}", file=sys.stderr)
                continue

            # Restore .git so we can optionally use it; then prune
            run(["git", "init"], cwd=submodule_clone)
            run(["git", "remote", "add", "origin", boost_repo_url], cwd=submodule_clone)
            prune_to_doc_only(submodule_clone, paths_to_keep)

            cppdigest_repo_url = f"https://github.com/{org}/{submodule_name}.git"
            exists = repo_exists(org, submodule_name, token)
            target_repo = None  # repo dir to use for fetching master SHA

            if exists:
                # Clone CppDigest repo from docs_branch; wipe except .git; copy pruned content; commit & push
                dest_repo = os.path.join(cppdigest_work, submodule_name)
                target_repo = dest_repo
                try:
                    clone_repo_keep_git(cppdigest_repo_url, docs_branch, dest_repo, token=token)
                except subprocess.CalledProcessError:
                    run(["git", "clone", "--depth", "1", authed_url(cppdigest_repo_url, token), dest_repo])
                    run(["git", "checkout", "-b", docs_branch], cwd=dest_repo)

                for item in os.listdir(dest_repo):
                    if item == ".git":
                        continue
                    p = os.path.join(dest_repo, item)
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                for item in os.listdir(submodule_clone):
                    if item == ".git":
                        continue
                    src = os.path.join(submodule_clone, item)
                    dst = os.path.join(dest_repo, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)

                run(["git", "add", "-A"], cwd=dest_repo)
                run(["git", "status", "--short"], cwd=dest_repo)
                run(["git", "commit", "-m", f"Update the original documentation of {libs_ref}"], cwd=dest_repo, check=False)
                run(["git", "push", "origin", docs_branch], cwd=dest_repo,
                    env={**os.environ, "GITHUB_TOKEN": token})
            else:
                # Create repo; push pruned content from submodule_clone to docs_branch; create empty master
                create_repo(org, submodule_name, token)
                target_repo = submodule_clone
                run(["git", "init"], cwd=submodule_clone)
                run(["git", "remote", "remove", "origin"], cwd=submodule_clone, check=False)
                run(["git", "remote", "add", "origin", authed_url(cppdigest_repo_url, token)], cwd=submodule_clone)
                run(["git", "add", "-A"], cwd=submodule_clone)
                run(["git", "commit", "-m", f"Create the original documentation of {libs_ref}"], cwd=submodule_clone)
                run(["git", "branch", "-M", docs_branch], cwd=submodule_clone)
                run(["git", "push", "-u", "origin", docs_branch], cwd=submodule_clone,
                    env={**os.environ, "GITHUB_TOKEN": token})
                run(["git", "checkout", "--orphan", "master"], cwd=submodule_clone)
                run(["git", "rm", "-rf", "."], cwd=submodule_clone, check=False)
                run(["git", "commit", "--allow-empty", "-m", "Empty master branch"], cwd=submodule_clone, check=False)
                run(["git", "push", "origin", "master"], cwd=submodule_clone,
                    env={**os.environ, "GITHUB_TOKEN": token})

            # 3) Update boost-documentation-translations: ensure libs/<submodule> points to CppDigest/<submodule> master
            if i == 1:
                trans_url = f"https://github.com/{org}/{translations_repo}.git"
                clone_repo_keep_git(trans_url, "main", translations_dir, token=token)
                run(["git", "config", "user.email", "ci@cppdigest.local"], cwd=translations_dir)
                run(["git", "config", "user.name", "CI"], cwd=translations_dir)

            if exists:
                run(["git", "fetch", "origin", "master"], cwd=target_repo)
            master_sha = run(
                ["git", "rev-parse", "origin/master"],
                cwd=target_repo,
                capture_output=True,
                text=True,
            ).stdout.strip()

            libs_path = os.path.join(translations_dir, "libs", submodule_name)
            if os.path.isdir(libs_path) and os.path.isdir(os.path.join(libs_path, ".git")):
                run(["git", "submodule", "update", "--init", f"libs/{submodule_name}"], cwd=translations_dir, check=False)
                run(["git", "-C", f"libs/{submodule_name}", "fetch", "origin", "master"], cwd=translations_dir)
                run(["git", "-C", f"libs/{submodule_name}", "checkout", master_sha], cwd=translations_dir)
                run(["git", "add", "libs/" + submodule_name], cwd=translations_dir)
            else:
                submodule_url = f"https://github.com/{org}/{submodule_name}.git"
                run(["git", "submodule", "add", "-b", "master", authed_url(submodule_url, token), f"libs/{submodule_name}"], cwd=translations_dir)
                run(["git", "config", f"submodule.libs/{submodule_name}.url", submodule_url], cwd=translations_dir)
                run(["git", "-C", f"libs/{submodule_name}", "checkout", master_sha], cwd=translations_dir)
                run(["git", "add", "libs/" + submodule_name], cwd=translations_dir)

        # Single commit for all submodule updates at end
        run(["git", "status", "--short"], cwd=translations_dir)
        run(["git", "commit", "-m", f"Update libs submodules to {libs_ref}"], cwd=translations_dir, check=False)
        run(["git", "push", "origin", "main"], cwd=translations_dir,
            env={**os.environ, "GITHUB_TOKEN": token})

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
