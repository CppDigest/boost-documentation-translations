#!/usr/bin/env python3
"""
Sync Boost library documentation from boostorg/boost to CppDigest organization.

Triggered by CI (repository_dispatch, event add-submodule). Submodule list: pass
--submodules as a list-like string (e.g. [algorithm, system]), or fetch
.gitmodules from https://github.com/boostorg/boost (ref = specified version or master).
For each libs/ submodule:
1. Clone boostorg repo at given ref; keep only doc folders per meta/libraries.json.
2. Update or create CppDigest/<submodule>: push doc content to master; then ensure
   local branch exists and, if there is no open PR from boost-<sub>-<lang>-translation-<version>,
   rebase local onto master.
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
MASTER_BRANCH = "master"
LOCAL_BRANCH = "local"
TRANSLATIONS_MASTER_BRANCH = "master"
TRANSLATIONS_LOCAL_BRANCH = "local"
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
    except URLError as e:
        print(f"URLError fetching libraries.json: {e}", file=sys.stderr)
        return []

    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"JSONDecodeError parsing libraries.json: {e}", file=sys.stderr)
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
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": USER_AGENT,
    }
    req = Request(url, headers=headers)
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


def set_default_branch(org: str, repo: str, branch: str, token: str) -> None:
    """Set the repository default branch via GitHub API (PATCH /repos/{org}/{repo})."""
    url = f"{GITHUB_API_BASE}/repos/{org}/{repo}"
    body = json.dumps({"name": repo, "default_branch": branch}).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="PATCH",
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
        print(f"Warning: set default branch to {branch} failed: {e.code} {resp_body}", file=sys.stderr)


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


def parse_submodules_list(s: str) -> List[str]:
    """
    Parse a list-like string into submodule names.
    E.g. '[algorithm]' -> ['algorithm'], '[algorithm, system]' -> ['algorithm', 'system'].
    """
    if not s or not s.strip():
        return []
    s = s.strip()
    if s.startswith("["):
        s = s[1:]
    if s.endswith("]"):
        s = s[:-1]
    return [name.strip() for name in s.split(",") if name.strip()]


def first_segments(paths: Set[str]) -> Set[str]:
    """For paths like doc, minmax/doc, string/doc return first segment set: doc, minmax, string."""
    segs: Set[str] = set()
    for p in paths:
        if not p:
            continue
        seg = p.split("/")[0]
        segs.add(seg)
    return segs


def prune_to_doc_only(clone_dir: str, paths_to_keep: Set[str]) -> None:
    """
    Remove all folders except doc-related paths. Keep doc folders and all root path files.
    Removes .git and .github too; caller runs git init again before add/commit/push.
    paths_to_keep is e.g. {"doc", "minmax/doc", "string/doc"}.
    """
    def prune_dir(base: str, keep_paths: Set[str]) -> None:
        current_segments = first_segments(keep_paths) if keep_paths else set()
        full_base = os.path.join(clone_dir, base) if base else clone_dir
        if not os.path.isdir(full_base):
            return
        for name in os.listdir(full_base):
            path = os.path.join(full_base, name)
            rel = f"{base}/{name}" if base else name
            if rel.startswith("/"):
                rel = rel[1:]
            if os.path.isfile(path):
                # Keep files under a keep path, or any file at repo root
                under = any(
                    rel == p or rel.startswith(p + "/")
                    for p in paths_to_keep
                )
                if not under and base != "":
                    os.remove(path)
            else:
                if name not in current_segments and rel not in paths_to_keep:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    sub_paths = {
                        p[len(rel):].lstrip("/")
                        for p in keep_paths
                        if p == rel or p.startswith(rel + "/")
                    }
                    prune_dir(rel, sub_paths)

    prune_dir("", paths_to_keep)


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
    clone_repo_keep_git(trans_url, TRANSLATIONS_MASTER_BRANCH, translations_dir, token=token)
    run(["git", "fetch", "origin"], cwd=translations_dir, check=False)
    run(["git", "config", "user.email", "Boost-Translation-CI-Bot@cppdigest.local"], cwd=translations_dir)
    run(["git", "config", "user.name", "Boost-Translation-CI-Bot"], cwd=translations_dir)


def get_master_and_local_shas(target_repo: str, token: str) -> Tuple[str, str]:
    """Resolve master and local branch SHAs for the target repo. Returns (master_sha, local_sha).
    target_repo is the path to a clone of a CppDigest library repo, e.g. /tmp/xyz/cppdigest/algorithm."""
    run(["git", "fetch", "origin", MASTER_BRANCH], cwd=target_repo, check=False)
    rev_master = run(
        ["git", "rev-parse", f"origin/{MASTER_BRANCH}"],
        cwd=target_repo,
        check=False,
    )
    if rev_master.returncode != 0:
        raise RuntimeError(f"Failed to get master SHA for {target_repo}")
    run(["git", "fetch", "origin", LOCAL_BRANCH], cwd=target_repo, check=False)
    rev_local = run(
        ["git", "rev-parse", f"origin/{LOCAL_BRANCH}"],
        cwd=target_repo,
        check=False,
    )
    if rev_local.returncode != 0:
        raise RuntimeError(f"Failed to get local SHA for {target_repo}")
        
    return (rev_master.stdout.strip(), rev_local.stdout.strip())


def has_open_translation_pr(
    org: str,
    repo: str,
    libs_ref: str,
    token: str,
    lang_code: Optional[str] = None,
) -> bool:
    """True if repo has an open PR from boost-<sub>-<language_code>-translation-<version>."""
    url = f"{GITHUB_API_BASE}/repos/{org}/{repo}/pulls?state=open"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"Error checking open PRs: {e}", file=sys.stderr)
        return False
    if lang_code:
        pattern = re.compile(
            r"^boost-" + re.escape(repo) + r"-" + re.escape(lang_code) + r"-translation-.+$",
            re.IGNORECASE,
        )
    else:
        pattern = re.compile(
            r"^boost-" + re.escape(repo) + r"-.+-translation-.+$", re.IGNORECASE
        )
    for pr in data:
        ref = (pr.get("head") or {}).get("ref") or ""
        if pattern.match(ref):
            return True
    return False


def update_local_rebase_onto_master(
    repo_dir: str,
    token: str,
    repo_url: str,
) -> None:
    """Update local branch by rebasing onto origin/master, then push (--force)."""
    run(["git", "fetch", "origin", MASTER_BRANCH], cwd=repo_dir)
    run(["git", "fetch", "origin", LOCAL_BRANCH], cwd=repo_dir, check=False)
    rev_local = run(
        ["git", "rev-parse", f"origin/{LOCAL_BRANCH}"],
        cwd=repo_dir,
        check=False,
    )
    if rev_local.returncode != 0:
        run(
            ["git", "checkout", "-b", LOCAL_BRANCH, f"origin/{MASTER_BRANCH}"],
            cwd=repo_dir,
        )
    else:
        run(
            ["git", "checkout", "-B", LOCAL_BRANCH, f"origin/{LOCAL_BRANCH}"],
            cwd=repo_dir,
        )
        run(["git", "rebase", f"origin/{MASTER_BRANCH}"], cwd=repo_dir)
    push_url = authed_url(repo_url, token)
    run(
        ["git", "push", "--force", push_url, f"{LOCAL_BRANCH}:{LOCAL_BRANCH}"],
        cwd=repo_dir,
        env={**os.environ, "GITHUB_TOKEN": token},
    )


def sync_existing_repo(
    dest_repo: str,
    submodule_clone: str,
    master_branch: str,
    libs_ref: str,
    org: str,
    submodule_name: str,
    token: str,
    repo_url: str,
    lang_code: Optional[str] = None,
) -> None:
    """Wipe dest_repo (except .git), copy from submodule_clone, commit and push.
    If no open PR from boost-<sub>-<language_code>-translation-<version>, rebase local onto master."""
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
    run(
        ["git", "commit", "-m", f"Update the original documentation of {libs_ref}"],
        cwd=dest_repo,
        check=False,
    )
    run(
        ["git", "push", "origin", master_branch],
        cwd=dest_repo,
        env={**os.environ, "GITHUB_TOKEN": token},
    )
    if has_open_translation_pr(org, submodule_name, libs_ref, token, lang_code=lang_code):
        return
    update_local_rebase_onto_master(dest_repo, token, repo_url)


def create_new_repo_and_push(
    org: str,
    submodule_name: str,
    submodule_clone: str,
    cppdigest_repo_url: str,
    libs_ref: str,
    token: str,
) -> None:
    """Create CppDigest repo, push docs to master, create local branch and push."""
    create_repo(org, submodule_name, token)
    run(["git", "init"], cwd=submodule_clone)
    run(["git", "config", "user.email", "ci@cppdigest.local"], cwd=submodule_clone)
    run(["git", "config", "user.name", "CI"], cwd=submodule_clone)
    run(["git", "add", "-A"], cwd=submodule_clone)
    run(["git", "commit", "-m", f"Create the original documentation of {libs_ref}"], cwd=submodule_clone)
    run(["git", "branch", "-M", MASTER_BRANCH], cwd=submodule_clone)
    run(["git", "remote", "remove", "origin"], cwd=submodule_clone, check=False)
    run(["git", "remote", "add", "origin", authed_url(cppdigest_repo_url, token)], cwd=submodule_clone)
    run(["git", "push", "-u", "origin", MASTER_BRANCH], cwd=submodule_clone,
        env={**os.environ, "GITHUB_TOKEN": token})
    run(["git", "checkout", "-b", LOCAL_BRANCH], cwd=submodule_clone)
    run(["git", "push", "-u", "origin", LOCAL_BRANCH], cwd=submodule_clone,
        env={**os.environ, "GITHUB_TOKEN": token})
    set_default_branch(org, submodule_name, MASTER_BRANCH, token)


def update_translations_submodule(
    translations_dir: str,
    org: str,
    submodule_name: str,
    sha: str,
    token: str,
) -> None:
    """Point libs/<submodule_name> at the given SHA; add submodule if needed."""
    libs_path = os.path.join(translations_dir, "libs", submodule_name)
    submodule_path = f"libs/{submodule_name}"

    def init_fetch_checkout_add() -> None:
        run(
            ["git", "submodule", "update", "--init", submodule_path],
            cwd=translations_dir,
            check=False,
        )
        run(
            ["git", "-C", submodule_path, "fetch", "origin"],
            cwd=translations_dir,
            check=False,
        )
        run(["git", "-C", submodule_path, "checkout", sha], cwd=translations_dir)
        run(["git", "add", submodule_path], cwd=translations_dir)

    if os.path.isdir(libs_path) and os.path.isdir(os.path.join(libs_path, ".git")):
        init_fetch_checkout_add()
    elif os.path.isdir(libs_path):
        # Path exists but not inited (submodule entry in index); init instead of add.
        init_fetch_checkout_add()
    else:
        submodule_url = f"https://github.com/{org}/{submodule_name}.git"
        run(
            [
                "git", "submodule", "add", "-b", MASTER_BRANCH,
                authed_url(submodule_url, token),
                submodule_path,
            ],
            cwd=translations_dir,
        )
        run(
            ["git", "config", f"submodule.{submodule_path}.url", submodule_url],
            cwd=translations_dir,
        )
        run(
            ["git", "config", "-f", ".gitmodules", f"submodule.{submodule_path}.url", submodule_url],
            cwd=translations_dir,
        )
        run(["git", "add", ".gitmodules"], cwd=translations_dir)
        run(
            ["git", "-C", submodule_path, "fetch", "origin"],
            cwd=translations_dir,
            check=False,
        )
        run(["git", "-C", submodule_path, "checkout", sha], cwd=translations_dir)
        run(["git", "add", submodule_path], cwd=translations_dir)


def _commit_and_push_translations_branch(
    translations_dir: str,
    branch: str,
    libs_ref: str,
    token: str,
    force_push: bool = False,
) -> None:
    """Commit submodule updates and push the current branch."""
    run(["git", "status", "--short"], cwd=translations_dir)
    run(
        ["git", "commit", "-m", f"Update libs submodules to {libs_ref}"],
        cwd=translations_dir,
        check=False,
    )
    push_cmd = ["git", "push", "origin", branch]
    if force_push:
        push_cmd.insert(2, "--force")  # after "push", before remote
    run(
        push_cmd,
        cwd=translations_dir,
        env={**os.environ, "GITHUB_TOKEN": token},
    )


def finalize_translations_repo(
    translations_dir: str,
    libs_ref: str,
    token: str,
    updates_master: List[Tuple[str, str]],
    updates_local: List[Tuple[str, str]],
    org: str,
) -> None:
    """Update boost-documentation-translations on master and local branches per prompt (3) and (4)."""
    if not updates_master and not updates_local:
        return
    run(["git", "fetch", "origin"], cwd=translations_dir, check=False)
    rev_master = run(
        ["git", "rev-parse", f"origin/{TRANSLATIONS_MASTER_BRANCH}"],
        cwd=translations_dir,
        check=False,
    )
    if rev_master.returncode == 0:
        run(
            [
                "git", "checkout", "-B", TRANSLATIONS_MASTER_BRANCH,
                f"origin/{TRANSLATIONS_MASTER_BRANCH}",
            ],
            cwd=translations_dir,
        )
    for submodule_name, sha in updates_master:
        update_translations_submodule(translations_dir, org, submodule_name, sha, token)
    _commit_and_push_translations_branch(
        translations_dir, TRANSLATIONS_MASTER_BRANCH, libs_ref, token,
        force_push=False,
    )
    rev_local = run(
        ["git", "rev-parse", f"origin/{TRANSLATIONS_LOCAL_BRANCH}"],
        cwd=translations_dir,
        check=False,
    )
    if rev_local.returncode == 0:
        run(
            [
                "git", "checkout", "-B", TRANSLATIONS_LOCAL_BRANCH,
                f"origin/{TRANSLATIONS_LOCAL_BRANCH}",
            ],
            cwd=translations_dir,
        )
    for submodule_name, sha in updates_local:
        update_translations_submodule(translations_dir, org, submodule_name, sha, token)
    _commit_and_push_translations_branch(
        translations_dir, TRANSLATIONS_LOCAL_BRANCH, libs_ref, token,
        force_push=True,
    )


def process_one_submodule(
    submodule_name: str,
    libs_ref: str,
    org: str,
    boost_work: str,
    cppdigest_work: str,
    token: str,
    lang_code: Optional[str] = None,
) -> Optional[Tuple[str, bool]]:
    """
    Clone boost submodule, prune to docs, update or create CppDigest repo.
    Returns (target_repo, exists) for get_master_and_local_shas and finalize, or None.
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
    prune_to_doc_only(submodule_clone, paths_to_keep)

    cppdigest_repo_url = f"https://github.com/{org}/{submodule_name}.git"
    exists = repo_exists(org, submodule_name, token)

    if exists:
        dest_repo = os.path.join(cppdigest_work, submodule_name)
        try:
            clone_repo_keep_git(cppdigest_repo_url, MASTER_BRANCH, dest_repo, token=token)
        except subprocess.CalledProcessError as e:
            print(f"  clone_repo_keep_git failed: {e}", file=sys.stderr)
            run(
                [
                    "git", "clone", "--depth", "1", "--branch", MASTER_BRANCH,
                    authed_url(cppdigest_repo_url, token), dest_repo,
                ],
            )
        sync_existing_repo(
            dest_repo, submodule_clone, MASTER_BRANCH, libs_ref,
            org, submodule_name, token, cppdigest_repo_url,
            lang_code=lang_code,
        )
        return (dest_repo, True)
    else:
        create_new_repo_and_push(
            org, submodule_name, submodule_clone, cppdigest_repo_url, libs_ref, token
        )
        return (submodule_clone, False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Boost docs to CppDigest")
    parser.add_argument("--gitmodules-ref", default="master", help="Ref for .gitmodules (e.g. master)")
    parser.add_argument("--libs-ref", default="develop", help="Ref for libs (e.g. boost-1.90.0 or develop)")
    parser.add_argument("--org", default="CppDigest", help="Target organization")
    parser.add_argument(
        "--translations-repo",
        default="boost-documentation-translations",
        help="Repo holding submodule links",
    )
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token")
    parser.add_argument(
        "--submodules",
        default="",
        metavar="LIST",
        help="List-like string of submodule names (e.g. [algorithm, system]). "
             "If empty, fetch .gitmodules from boostorg/boost.",
    )
    parser.add_argument(
        "--lang-code",
        default="",
        metavar="LANG",
        help="Language code for translation PR branch (e.g. zh_Hans). "
             "When set, only PRs from boost-<sub>-<lang_code>-translation-<version> are considered.",
    )
    args = parser.parse_args()

    if not args.token:
        print("Error: GITHUB_TOKEN or --token required", file=sys.stderr)
        sys.exit(1)

    token = args.token
    org = args.org
    translations_repo = args.translations_repo

    submodule_names = parse_submodules_list(args.submodules)
    if submodule_names:
        lib_submodules = [(name, f"libs/{name}") for name in submodule_names]
        print(f"Using {len(lib_submodules)} submodules from input.", file=sys.stderr)
    else:
        lib_submodules = get_lib_submodules(args.gitmodules_ref, token)

    with tempfile.TemporaryDirectory() as work:
        boost_work = os.path.join(work, "boost")
        cppdigest_work = os.path.join(work, "cppdigest")
        translations_dir = os.path.join(work, "translations")
        os.makedirs(boost_work, exist_ok=True)
        os.makedirs(cppdigest_work, exist_ok=True)
        updates_master: List[Tuple[str, str]] = []
        updates_local: List[Tuple[str, str]] = []

        for i, (submodule_name, _path_in_boost) in enumerate(lib_submodules, 1):
            print(f"[{i}/{len(lib_submodules)}] {submodule_name} ...", file=sys.stderr)
            lang_code = args.lang_code.strip() or None
            result = process_one_submodule(
                submodule_name, args.libs_ref, org,
                boost_work, cppdigest_work, token,
                lang_code=lang_code,
            )
            if result is None:
                continue
            target_repo, exists = result
            ensure_translations_cloned(org, translations_repo, translations_dir, token)
            cppdigest_repo_url = f"https://github.com/{org}/{submodule_name}.git"
            try:
                master_sha, local_sha = get_master_and_local_shas(target_repo, token)
            except (RuntimeError, subprocess.CalledProcessError) as e:
                print(f"  Skipping submodule pointer update: {e}", file=sys.stderr)
                continue
            updates_master.append((submodule_name, master_sha))
            updates_local.append((submodule_name, local_sha))

        if os.path.isdir(os.path.join(translations_dir, ".git")):
            finalize_translations_repo(
                translations_dir, args.libs_ref, token,
                updates_master, updates_local, org,
            )

    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
