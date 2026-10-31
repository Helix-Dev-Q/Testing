"""Looks for new Fortnite Xbox builds and adds them to the README."""
import argparse
import collections
import concurrent.futures
import datetime
import email.utils
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import render
import xbl

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / ".xbox-refresh-token"

CATALOG_URL = (
    "https://displaycatalog.mp.microsoft.com/v7.0/products"
    "?bigIds=BT5P2X999VH2&market=US&languages=en-US&fieldsTemplate=details"
)
EPIC_VERSION_URL = "https://fortnite-public-service-prod11.ol.epicgames.com/fortnite/api/version"
FN_RELEASES_URL = "https://raw.githubusercontent.com/polynite/fn-releases/master/README.md"

# Both Xbox packages share one content ID, so only their rank tells them apart in the catalog.
XBOX_ONE_RANK = 50000
XBOX_SERIES_RANK = 51000
CONTENT_IDS = {
    "Xbox": "4a617fcb-3c09-4ac5-9c4c-dc46b5263876",
    "PC": "21b40136-2fdb-404b-be34-4656e6a88528",
}

WALK_DELAY_SECONDS = 0.25
MAX_WALK_LOOKUPS = 1000

# Xbox won't give this login Series files, so these come from XboxDownload's lookup, with its
# developer's permission. The user agent still names this project.
SERIES_LOOKUP_URL = "https://xbox.skydevil.xyz/Game/GetGamePackage?contentId={content_id}&platform=1&size={size}"
SERIES_LOOKUP_HEADERS = {
    "X-Organization": "XboxDownload",
    "X-Author": "Devil",
    "User-Agent": xbl.USER_AGENT,
    "Accept": "application/json",
}
SERIES_RETRY_DAYS = 2

FN_RELEASES_SEASON = re.compile(r"^# Season (\d+)", re.M)
FN_RELEASES_BUILD = re.compile(r"^\|\s*(\S+-CL-(\d+))\s*\|", re.M)


def fetch(url, attempts=3):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read().decode("utf-8")
        except OSError as error:
            if attempt == attempts - 1:
                print(f"::warning::Couldn't fetch {url}: {error}")
                return None
            time.sleep(2**attempt)


def fetch_live():
    """Reads the public store and Epic data. Neither needs a sign-in."""
    live = {}
    epic = fetch(EPIC_VERSION_URL)
    if epic:
        epic = json.loads(epic)
        live["version"] = epic["version"]
        live["server_cl"] = int(epic["cln"])

    catalog = fetch(CATALOG_URL)
    if catalog:
        sizes = {}
        for availability in json.loads(catalog)["Products"][0]["DisplaySkuAvailabilities"]:
            for package in availability["Sku"]["Properties"].get("Packages") or []:
                if package["PackageFormat"] == "MSIXVC":
                    sizes["pc"] = package["MaxDownloadSizeInBytes"]
                elif package["PackageRank"] == XBOX_ONE_RANK:
                    sizes["xbox_one"] = package["MaxDownloadSizeInBytes"]
                elif package["PackageRank"] == XBOX_SERIES_RANK:
                    sizes["xbox_series"] = package["MaxDownloadSizeInBytes"]
        live["store_sizes"] = sizes
    return live


def fetch_known_names():
    """Maps changelists to their build name and season using fn-releases."""
    text = fetch(FN_RELEASES_URL)
    if not text:
        return {}
    names = {}
    seasons = list(FN_RELEASES_SEASON.finditer(text))
    for index, heading in enumerate(seasons):
        end = seasons[index + 1].start() if index + 1 < len(seasons) else len(text)
        for match in FN_RELEASES_BUILD.finditer(text, heading.end(), end):
            names[int(match[2])] = (match[1], int(heading[1]))
    return names


def head(url):
    """Returns (status, size, ISO date) from the CDN without downloading the file."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            modified = response.headers.get("Last-Modified")
            date = email.utils.parsedate_to_datetime(modified).date().isoformat() if modified else None
            size = int(response.headers.get("Content-Length") or 0)
            return response.status, size, date
    except urllib.error.HTTPError as error:
        return error.code, None, None
    except OSError:
        return None, None, None


def check_links(db):
    """Marks links that the CDN no longer serves. Network errors leave the old state alone."""
    packages = [package for build in db["builds"] for package in build["packages"].values()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda package: head(package["url"]), packages))
    for package, (status, size, _) in zip(packages, results):
        if status == 200:
            package["alive"] = True
            package["size"] = size or package.get("size")
        elif status in (403, 404, 410):
            package["alive"] = False


def guess_version(cl, names, db):
    """Uses the live version for a new build, or the nearest older version for an old one."""
    live_version = db["live"]["version"]
    live = (live_version, int(live_version.split(".")[0]))
    if cl > max((build["cl"] for build in db["builds"]), default=0):
        return live
    known = {build["cl"]: (build["build"], build["season"]) for build in db["builds"] if not build.get("inferred")}
    known |= names
    older = [key for key, (name, _) in known.items() if key < cl and name[0].isdigit()]
    if not older:
        return live
    name, season = known[max(older)]
    return name.rsplit("-CL-", 1)[0], season


def new_build(cl, names, db):
    if cl in names:
        name, season = names[cl]
        return {"build": name, "cl": cl, "season": season, "date": None, "packages": {}, "notes": ""}
    version, season = guess_version(cl, names, db)
    return {
        "build": f"{version}-CL-{cl}",
        "cl": cl,
        "season": season,
        "date": None,
        "packages": {},
        "notes": "",
        "inferred": True,
    }


def add_package(db, file_name, size, url, names):
    """Adds one CDN file to the list. Returns True if it was new."""
    parsed = render.parse_file_name(file_name)
    if not parsed or parsed[1] is None:
        print(f"::warning::Skipping {file_name}: can't read a build from the name.")
        return False
    platform, cl = parsed

    build = next((build for build in db["builds"] if build["cl"] == cl), None)
    if build and build["packages"].get(platform, {}).get("url") == url:
        return False
    if not build:
        build = new_build(cl, names, db)
        db["builds"].append(build)

    status, head_size, date = head(url)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    # Only the newest build can safely fall back to today, since an old link may be dead.
    fallback = today if cl == max(other["cl"] for other in db["builds"]) else None
    build["date"] = build["date"] or date or fallback
    build["packages"][platform] = {
        "url": url,
        "size": size or head_size,
        "alive": status == 200 if status else True,
    }
    print(f"New: {build['build']} ({platform})")
    return True


def has_version(db, version):
    return any(
        f"/{version}." in package["url"] for build in db["builds"] for package in build["packages"].values()
    )


def collect_older_builds(db, label, content_id, package, auth_header, names, walk=False):
    """Follows update files back through older builds, since each package lists ones before it.

    A normal run only opens builds we don't have yet. A walk opens every build, to reach the ones
    hiding behind builds we already have, so it's slow and meant to be run by hand.
    """
    queue = collections.deque(xbl.older_version_ids(package))
    seen = set(queue)
    lookups = 0
    while queue and lookups < MAX_WALK_LOOKUPS:
        version_id = queue.popleft()
        if not walk and has_version(db, version_id.rsplit(".", 1)[0]):
            continue
        lookups += 1
        if walk:
            time.sleep(WALK_DELAY_SECONDS)
        try:
            older = xbl.get_specific_package(content_id, version_id, auth_header)
        except (xbl.PackageDenied, OSError) as error:
            print(f"::warning::{label} {version_id} lookup failed: {error}")
            continue
        if not older:
            continue
        for file_name, size, url in xbl.package_files(older):
            add_package(db, file_name, size, url, names)
        for older_id in xbl.older_version_ids(older):
            if older_id not in seen:
                seen.add(older_id)
                queue.append(older_id)
    if walk:
        print(f"{label} walk: {lookups} lookups, {len(seen)} versions seen")


def collect_builds(db, token, names, walk=False):
    tokens = xbl.refresh(token)
    save_rotated_token(tokens.get("refresh_token"))
    auth_header = xbl.update_auth_header(tokens["access_token"])
    denied = 0
    for label, content_id in CONTENT_IDS.items():
        try:
            package = xbl.get_base_package(content_id, auth_header)
        except xbl.PackageDenied as error:
            print(f"::warning::{label} package refused: {error}")
            denied += 1
            continue
        except OSError as error:
            print(f"::warning::{label} package lookup failed: {error}")
            continue
        if not package:
            print(f"::warning::No {label} package returned.")
            continue
        files = xbl.package_files(package)
        print(f"{label} package found: {len(files)} file(s)")
        for file_name, size, url in files:
            add_package(db, file_name, size, url, names)
        collect_older_builds(db, label, content_id, package, auth_header, names, walk)
    if denied == len(CONTENT_IDS):
        raise xbl.AuthError("The package API refused every package for this account.")


def series_lookup_due(db, newest_before, now):
    """Allows three lookups per build: when it appears, then once a day for two days."""
    newest = max(db["builds"], key=lambda build: build["cl"])
    if "xbox_series" in newest["packages"]:
        return False
    if newest["cl"] != newest_before:
        return True
    if not newest["date"] or now.hour != 0 or now.minute >= 5:
        return False
    age = (now.date() - datetime.date.fromisoformat(newest["date"])).days
    return 0 < age <= SERIES_RETRY_DAYS


def fetch_series_link(db, names):
    """Adds the newest Series link from XboxDownload, once the real CDN confirms it."""
    size = db["live"].get("store_sizes", {}).get("xbox_series")
    if not size:
        return False
    url = SERIES_LOOKUP_URL.format(content_id=CONTENT_IDS["Xbox"], size=size)
    request = urllib.request.Request(url, headers=SERIES_LOOKUP_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.load(response)
    except (OSError, ValueError) as error:
        print(f"::warning::Series lookup failed: {error}")
        return False

    link = (answer.get("data") or {}).get("url") if str(answer.get("code")) == "200" else None
    if not link:
        print("Series lookup: no link available yet.")
        return False
    parts = link.split("/")
    file_name = parts[-1]
    parsed = render.parse_file_name(file_name)
    expected = len(parts) > 6 and parts[2] == "assets1.xboxlive.com" and parts[5] == CONTENT_IDS["Xbox"]
    if not expected or not parsed or parsed[0] != "xbox_series" or not parsed[1]:
        print(f"::warning::Series lookup returned an unexpected link: {file_name}")
        return False
    if head(link)[0] != 200:
        print(f"::warning::Series lookup link isn't on the CDN: {file_name}")
        return False
    return add_package(db, file_name, None, link, names)


def save_rotated_token(token):
    target = os.environ.get("XBOX_TOKEN_OUT") or (TOKEN_FILE if TOKEN_FILE.exists() else None)
    if token and target:
        Path(target).write_text(token, encoding="utf-8")


def load_token():
    token = os.environ.get("XBOX_REFRESH_TOKEN")
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8")
    return token.strip() if token else None


def rename_inferred(db, names):
    """Swaps guessed names for real ones once fn-releases lists the changelist."""
    for build in db["builds"]:
        if build.get("inferred") and build["cl"] in names:
            build["build"], build["season"] = names[build["cl"]]
            del build["inferred"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-links", action="store_true", help="also re-check every CDN link")
    parser.add_argument("--walk-history", action="store_true", help="open every older build to find the ones behind it")
    args = parser.parse_args()

    db = render.read_readme()
    db["live"] = db["live"] | fetch_live()
    names = fetch_known_names()
    newest_before = max(build["cl"] for build in db["builds"])
    failed = False

    token = load_token()
    if token:
        try:
            collect_builds(db, token, names, args.walk_history)
        except xbl.AuthError as error:
            print(f"::error::{error}")
            failed = True
        except OSError as error:
            print(f"::warning::Couldn't reach Xbox Live, will try again next run: {error}")
    else:
        print("::warning::No sign-in saved, so only public data was updated. Run scripts/signin.py.")

    if series_lookup_due(db, newest_before, datetime.datetime.now(datetime.timezone.utc)):
        fetch_series_link(db, names)

    rename_inferred(db, names)
    if args.check_links:
        check_links(db)

    db["builds"].sort(key=lambda build: build["cl"] or 0)
    render.write_readme(db)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
