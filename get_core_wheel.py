"""
Install or update the compiled parser-core wheel - the kb equivalent of
calcpkg's get_latest.py.

Picks the release asset that matches this interpreter (cp3XX) and platform
(win_amd64) from the core releases repo, and pip-installs it.  The rest of the
knowledge base is the source tree alongside this file; once the wheel is in,
kb.parse / kb.index / kb.lexicon resolve to the compiled modules.

    python get_core_wheel.py

No token is needed while the releases repo is public.  If you make it private,
set a fine-grained PAT with Contents: read on it first:

    set GITHUB_TOKEN=github_pat_...        (Windows)
    python get_core_wheel.py
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request

# The PUBLIC repo the CI workflow publishes the wheel to.  Must match
# PUBLIC_REPO in .github/workflows/release.yml - that workflow rewrites this
# line when it builds the public tree, so the copy a client clones is always
# correct and this value only matters when running from the private repo.
RELEASES_REPO = "VaraprasadG-AidenAI/Alphaplus_kb_releases"
DIST_NAME = "alphaplus_kb_core"


def log(msg: str) -> None:
    print(f"[core-wheel] {msg}")


def platform_tag() -> str:
    return {"Windows": "win_amd64",
            "Linux": "linux_x86_64",
            "Darwin": "macosx"}.get(platform.system(), "")


def python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def installed_version() -> str | None:
    from importlib.metadata import version, PackageNotFoundError
    try:
        return version(DIST_NAME)
    except PackageNotFoundError:
        return None


def _request(url: str) -> urllib.request.Request:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def latest_matching(py_tag: str, plat_tag: str):
    url = f"https://api.github.com/repos/{RELEASES_REPO}/releases"
    log("checking for the latest compiled core...")
    with urllib.request.urlopen(_request(url), timeout=15) as resp:
        releases = json.loads(resp.read().decode())
    for release in releases:                       # newest first
        for asset in release.get("assets", []):
            name = asset["name"]
            if name.endswith(".whl") and py_tag in name and plat_tag in name:
                return release["tag_name"].removeprefix("core-").lstrip("v"), \
                    name, asset["url"]
    return None, None, None


def download(api_asset_url: str, dest: str) -> None:
    # The asset API URL needs the octet-stream Accept header to return bytes.
    req = _request(api_asset_url)
    req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())


def main() -> int:
    py_tag, plat_tag = python_tag(), platform_tag()
    if not plat_tag:
        log(f"unsupported platform: {platform.system()}")
        return 1
    current = installed_version()
    log(f"installed: {current or 'none'}   interpreter: {py_tag}-{plat_tag}")

    try:
        latest, asset_name, asset_url = latest_matching(py_tag, plat_tag)
    except urllib.error.HTTPError as exc:
        log(f"could not read releases ({exc.code}). A private repo needs "
            f"GITHUB_TOKEN set.")
        return 1

    if not asset_url:
        log("no release matches this interpreter/platform.")
        return 1
    if current == latest:
        log(f"already up to date ({current}).")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        whl = os.path.join(tmp, asset_name)
        log(f"downloading {asset_name} ...")
        download(asset_url, whl)
        log("installing ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--upgrade", "--force-reinstall", "--quiet", whl])
    log(f"done: {current or 'none'} -> {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
