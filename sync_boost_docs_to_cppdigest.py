#!/usr/bin/env python3
"""
Sync Boost library documentation from boostorg/boost to CppDigest organization.

Triggered by CI (repository_dispatch). For each libs/ submodule:
1. Clone boostorg repo at given ref; keep only doc folders per meta/libraries.json.
2. Update or create CppDigest/<submodule> with that content on docs branch.
3. Update boost-documentation-translations submodule links in libs/ to point to
   latest commit of CppDigest/<submodule> master branch.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = "BoostDocsSync/1.0"
BOOST_ORG = "boostorg"
DOCS_BRANCH = "local"
GITHUB_API_BASE = "https://api.github.com"
GITMODULES_URL_TEMPLATE = "https://raw.githubusercontent.com/boostorg/boost/{ref}/.gitmodules"
LIBS_JSON_TEMPLATE = "https://raw.githubusercontent.com/boostorg/{repo}/{ref}/meta/libraries.json"
REPO_URL_TEMPLATE = "https://github.com/boostorg/{repo}.git"
GITMODULES_PATH_PREFIX = "path = "


def fetch_url(url: str, token: Optional[str] = None) -> str:
    """Fetch URL; return response body as string. Optional token for GitHub rate limit."""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def parse_gitmodules(content: str) -> List[Tuple[str, str]]:
    """Parse .gitmodules and return list of (submodule_name, path)."""
    entries = []
    current_name = None
    current_path = None
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r'\[submodule\s+"([^"]+)"\]', line)
        if m:
            if current_name is not None and current_path is not None:
                entries.append((current_name, current_path))
            current_name = m.group(1)
            current_path = None
            continue
        if line.startswith(GITMODULES_PATH_PREFIX):
            current_path = line[len(GITMODULES_PATH_PREFIX):].strip()
    if current_name is not None and current_path is not None:
        entries.append((current_name, current_path))
    return entries


def get_libraries_from_repo(
    submodule_name: str, ref: str, token: Optional[str] = None
) -> List[Tuple[str, str, str]]:
    """
    Fetch meta/libraries.json for a submodule at ref (branch/tag).
    Returns list of (first_column, repo_url, subpath).
    """
    url = LIBS_JSON_TEMPLATE.format(repo=submodule_name, ref=ref)
    try:
        content = fetch_url(url, token=token)
    except HTTPError as e:
        if e.code == 404:
            return []
        raise
    except URLError:
        return []

    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return []

    if isinstance(raw, list):
        libs = raw
    elif isinstance(raw, dict):
        libs = [raw]
    else:
        return []

    repo_url = REPO_URL_TEMPLATE.format(repo=submodule_name)
    result = []
    for obj in libs:
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("key", "")
        key = obj.get("key", "")
        if not name or not key:
            continue
        if key == submodule_name:
            first_column = key
            subpath = ""
        else:
            prefix = submodule_name + "/"
            first_column = name
            subpath = key[len(prefix):] if key.startswith(prefix) else key
        result.append((first_column, repo_url, subpath))
    return result


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
    body = json.dumps({"name": repo, "private": private}).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=30) as _:
            pass
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"Create repo failed: {e.code} {resp_body}") from e


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


def get_lib_submodules(gitmodules_ref: str, token: str) -> List[Tuple[str, str]]:
    """Fetch .gitmodules from boostorg/boost and return libs/ submodules (name, path)."""
    url = GITMODULES_URL_TEMPLATE.format(ref=gitmodules_ref)
    print(f"Fetching .gitmodules from boostorg/boost at {gitmodules_ref}...", file=sys.stderr)
    try:
        gitmodules = fetch_url(url, token=token)
    except (HTTPError, URLError) as e:
        print(f"Failed to fetch .gitmodules: {e}", file=sys.stderr)
        sys.exit(1)
    submodules = parse_gitmodules(gitmodules)
    lib_submodules = [(n, p) for n, p in submodules if p.startswith("libs/")]
    print(f"Found {len(lib_submodules)} libs submodules.", file=sys.stderr)
    return lib_submodules


def ensure_translations_cloned(
    org: str,
    translations_repo: str,
    translations_dir: str,
    token: str,
) -> None:
    """Clone translations repo and set git config if not already present."""
    if os.path.isdir(os.path.join(translations_dir, ".git")):
        return
    trans_url = f"https://github.com/{org}/{translations_repo}.git"
    clone_repo_keep_git(trans_url, "main", translations_dir, token=token)
    run(["git", "config", "user.email", "ci@cppdigest.local"], cwd=translations_dir)
    run(["git", "config", "user.name", "CI"], cwd=translations_dir)


def get_master_sha(
    target_repo: str,
    exists: bool,
    token: str,
    repo_url: Optional[str] = None,
) -> str:
    """Resolve the current master branch SHA for the target repo; create empty master if missing."""
    if exists:
        run(["git", "fetch", "origin", "master"], cwd=target_repo, check=False)
    rev_parse = run(
        ["git", "rev-parse", "origin/master"],
        cwd=target_repo,
        check=False,
    )
    if rev_parse.returncode != 0 and exists:
        run(["git", "checkout", "--orphan", "master"], cwd=target_repo)
        run(["git", "rm", "-rf", "."], cwd=target_repo, check=False)
        run(["git", "commit", "--allow-empty", "-m", "Empty master branch"], cwd=target_repo, check=False)
        if repo_url:
            push_url = authed_url(repo_url, token)
            run(["git", "push", push_url, "master"], cwd=target_repo)
            run(["git", "remote", "set-url", "origin", push_url], cwd=target_repo)
        else:
            run(["git", "push", "origin", "master"], cwd=target_repo,
                env={**os.environ, "GITHUB_TOKEN": token})
        run(["git", "fetch", "origin", "master"], cwd=target_repo)
        rev_parse = run(["git", "rev-parse", "origin/master"], cwd=target_repo)
    master_sha = rev_parse.stdout.strip()
    if not master_sha:
        raise RuntimeError(f"Failed to get master SHA for {target_repo}")
    return master_sha


def sync_existing_repo(
    dest_repo: str,
    submodule_clone: str,
    docs_branch: str,
    libs_ref: str,
    token: str,
) -> None:
    """Wipe dest_repo (except .git), copy from submodule_clone, commit and push to docs_branch."""
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
    run(["git", "config", "user.email", "ci@cppdigest.local"], cwd=dest_repo)
    run(["git", "config", "user.name", "CI"], cwd=dest_repo)
    run(["git", "add", "-A"], cwd=dest_repo)
    run(["git", "status", "--short"], cwd=dest_repo)
    run(["git", "commit", "-m", f"Update the original documentation of {libs_ref}"], cwd=dest_repo, check=False)
    run(["git", "push", "origin", docs_branch], cwd=dest_repo,
        env={**os.environ, "GITHUB_TOKEN": token})


def create_new_repo_and_push(
    org: str,
    submodule_name: str,
    submodule_clone: str,
    cppdigest_repo_url: str,
    docs_branch: str,
    libs_ref: str,
    token: str,
) -> None:
    """Create CppDigest repo, push pruned docs to docs_branch, then create and push empty master."""
    create_repo(org, submodule_name, token)
    run(["git", "init"], cwd=submodule_clone)
    run(["git", "config", "user.email", "ci@cppdigest.local"], cwd=submodule_clone)
    run(["git", "config", "user.name", "CI"], cwd=submodule_clone)
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


def update_translations_submodule(
    translations_dir: str,
    org: str,
    submodule_name: str,
    master_sha: str,
    token: str,
) -> None:
    """Point libs/<submodule_name> at the given master SHA; add submodule if needed."""
    libs_path = os.path.join(translations_dir, "libs", submodule_name)
    if os.path.isdir(libs_path) and os.path.isdir(os.path.join(libs_path, ".git")):
        run(["git", "submodule", "update", "--init", f"libs/{submodule_name}"], cwd=translations_dir, check=False)
        run(["git", "-C", f"libs/{submodule_name}", "fetch", "origin", "master"], cwd=translations_dir)
        run(["git", "-C", f"libs/{submodule_name}", "checkout", master_sha], cwd=translations_dir)
        run(["git", "add", "libs/" + submodule_name], cwd=translations_dir)
    else:
        submodule_url = f"https://github.com/{org}/{submodule_name}.git"
        run(["git", "submodule", "add", "-b", "master", authed_url(submodule_url, token),
            f"libs/{submodule_name}"], cwd=translations_dir)
        run(["git", "config", f"submodule.libs/{submodule_name}.url", submodule_url], cwd=translations_dir)
        run(["git", "-C", f"libs/{submodule_name}", "checkout", master_sha], cwd=translations_dir)
        run(["git", "add", "libs/" + submodule_name], cwd=translations_dir)


def finalize_translations_repo(
    translations_dir: str,
    libs_ref: str,
    token: str,
) -> None:
    """Commit and push all submodule updates in the translations repo."""
    run(["git", "status", "--short"], cwd=translations_dir)
    run(["git", "commit", "-m", f"Update libs submodules to {libs_ref}"], cwd=translations_dir, check=False)
    run(["git", "push", "origin", "main"], cwd=translations_dir,
        env={**os.environ, "GITHUB_TOKEN": token})


def process_one_submodule(
    submodule_name: str,
    libs_ref: str,
    docs_branch: str,
    org: str,
    boost_work: str,
    cppdigest_work: str,
    token: str,
) -> Optional[Tuple[str, bool]]:
    """
    Clone boost submodule, prune to docs, update or create CppDigest repo.
    Returns (target_repo, exists) for use with get_master_sha and update_translations_submodule, or None if skipped.
    """
    libs = get_libraries_from_repo(submodule_name, libs_ref, token=token)
    if not libs:
        print(f"  No libraries.json entries, skipping.", file=sys.stderr)
        return None
    paths_to_keep = doc_paths_to_keep(libs)
    if not paths_to_keep:
        print(f"  No doc paths, skipping.", file=sys.stderr)
        return None

    boost_repo_url = f"https://github.com/{BOOST_ORG}/{submodule_name}.git"
    submodule_clone = os.path.join(boost_work, submodule_name)
    try:
        clone_repo(boost_repo_url, libs_ref, submodule_clone, token=token)
    except subprocess.CalledProcessError as e:
        print(f"  Clone failed: {e.stderr}", file=sys.stderr)
        return None

    run(["git", "init"], cwd=submodule_clone)
    run(["git", "remote", "add", "origin", boost_repo_url], cwd=submodule_clone)
    prune_to_doc_only(submodule_clone, paths_to_keep)

    cppdigest_repo_url = f"https://github.com/{org}/{submodule_name}.git"
    exists = repo_exists(org, submodule_name, token)

    if exists:
        dest_repo = os.path.join(cppdigest_work, submodule_name)
        try:
            clone_repo_keep_git(cppdigest_repo_url, docs_branch, dest_repo, token=token)
        except subprocess.CalledProcessError:
            run(["git", "clone", "--depth", "1", "--branch", docs_branch,
                 authed_url(cppdigest_repo_url, token), dest_repo])
        sync_existing_repo(dest_repo, submodule_clone, docs_branch, libs_ref, token)
        return (dest_repo, True)
    else:
        create_new_repo_and_push(
            org, submodule_name, submodule_clone, cppdigest_repo_url, docs_branch, libs_ref, token
        )
        return (submodule_clone, False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Boost docs to CppDigest")
    parser.add_argument("--gitmodules-ref", default="master", help="Ref for .gitmodules (e.g. master)")
    parser.add_argument("--libs-ref", default="develop", help="Ref for libs (e.g. boost-1.90.0 or develop)")
    parser.add_argument("--org", default="CppDigest", help="Target organization")
    parser.add_argument("--translations-repo", default="boost-documentation-translations",
                        help="Repo holding submodule links")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    args = parser.parse_args()

    if not args.token:
        print("Error: GITHUB_TOKEN or --token required", file=sys.stderr)
        sys.exit(1)

    token = args.token
    org = args.org
    translations_repo = args.translations_repo
    docs_branch = DOCS_BRANCH

    lib_submodules = get_lib_submodules(args.gitmodules_ref, token)

    with tempfile.TemporaryDirectory() as work:
        boost_work = os.path.join(work, "boost")
        cppdigest_work = os.path.join(work, "cppdigest")
        translations_dir = os.path.join(work, "translations")
        os.makedirs(boost_work, exist_ok=True)
        os.makedirs(cppdigest_work, exist_ok=True)

        for i, (submodule_name, _path_in_boost) in enumerate(lib_submodules, 1):
            print(f"[{i}/{len(lib_submodules)}] {submodule_name} ...", file=sys.stderr)
            result = process_one_submodule(
                submodule_name, args.libs_ref, docs_branch, org,
                boost_work, cppdigest_work, token,
            )
            if result is None:
                continue
            target_repo, exists = result
            ensure_translations_cloned(org, translations_repo, translations_dir, token)
            cppdigest_repo_url = f"https://github.com/{org}/{submodule_name}.git"
            try:
                master_sha = get_master_sha(target_repo, exists, token, repo_url=cppdigest_repo_url)
            except RuntimeError as e:
                print(f"  {e}", file=sys.stderr)
                sys.exit(1)
            update_translations_submodule(translations_dir, org, submodule_name, master_sha, token)

        if os.path.isdir(os.path.join(translations_dir, ".git")):
            finalize_translations_repo(translations_dir, args.libs_ref, token)

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
