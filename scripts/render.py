"""Reads and writes README.md, which is the only place the build list is stored."""
import re
from itertools import groupby
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README_FILE = ROOT / "README.md"

PLATFORMS = {"xbox_one": "Xbox One", "xbox_series": "Xbox Series", "pc": "PC"}
HEADERS = ["Xbox Version", "PC Version", "Date", *PLATFORMS.values(), "Notes"]
SIZE_UNITS = {"MB": 1_000_000, "GB": 1_000_000_000}

SEASON_HEADING = re.compile(r"# (?:Season (\d+)|Pre-Battle Royale)")
LIVE_VERSION = re.compile(r"\*\*Game version:\*\* (\S+) \(server CL (\d+)\)")
SIZE = re.compile(r"([\d.]+) (GB|MB)")
LINK = re.compile(r"\[(.+)\]\((.+)\)")
CHANGELIST = re.compile(r"CL-(\d+)")
FILE_NAME = re.compile(r"_(\d+)\.(\d+)\.(\d+)\.(\d+)_(?:neutral|x64)__[a-z0-9]+(?:_(xs|x))?(?:\.\w+)?$")

INTRO = """\
# Fortnite Xbox Builds

All known Xbox builds of Fortnite with their CDN links, laid out like \
[fn-releases](https://github.com/polynite/fn-releases). A GitHub Actions job checks for new \
builds every 5 minutes, so this list keeps itself up to date.

If something is wrong or missing, open an issue or pull request.

> [!NOTE]
> These are encrypted `.xvc` and `.msixvc` packages. They aren't playable builds you can run on \
a normal PC.
"""

LEGEND = """\
## Info
- **Xbox Version / PC Version:** Game version and changelist, with the changelist read from that \
row's Xbox or PC link. The two differ when Xbox and PC got separate builds of the same update. `*` \
means the game version was worked out from neighbouring builds and may be slightly off.
- **Date:** When the package was uploaded, or the date from the original list. Blank if unknown.
- **Xbox One / Xbox Series / PC:** CDN link, labelled with the file size. `removed` means the \
link returned an error at the last check. Xbox's API currently only gives out the Xbox One and \
PC files, so many newer builds have no Xbox Series link.
"""

CREDITS = """\
## Credits
- Build history up to 42.30 comes from Xader726's \
[FortniteXboxBuilds](https://github.com/Xader726/FortniteXboxBuilds) and \
[xboxlinkcollection](https://github.com/Xader726/xboxlinkcollection).
- Newer Xbox Series links come from [XboxDownload](https://github.com/skydevil88/XboxDownload).
- Version names and the README layout come from \
[polynite/fn-releases](https://github.com/polynite/fn-releases).
"""


def size_text(size):
    if not size:
        return "Download"
    if size < SIZE_UNITS["GB"]:
        return f"{size / SIZE_UNITS['MB']:.1f} MB"
    return f"{size / SIZE_UNITS['GB']:.1f} GB"


def package_link(package):
    if not package:
        return ""
    label = "removed" if package.get("alive") is False else size_text(package.get("size"))
    return f"[{label}]({package['url']})"


def parse_file_name(name):
    """Returns (platform, changelist) for a package file name, or None if it doesn't match.

    Xbox packages look like 1.0.4321.4806 and PC packages like 1.4839.828.0, where joining the
    two middle numbers gives the changelist (43214806 and 48390828).
    """
    match = FILE_NAME.search(name)
    if not match:
        return None
    major, minor, high, low = (int(part) for part in match.groups()[:4])
    if name.lower().endswith(".msixvc"):
        platform = "pc"
        high, low = minor, high
    elif match[5] == "xs" or major == 2:
        platform = "xbox_series"
    else:
        platform = "xbox_one"
    return platform, int(f"{high}{low:04d}") if high else None


def link_changelist(package):
    """Returns the changelist in a link's file name, or None if it doesn't have one."""
    parsed = parse_file_name(package["url"].rsplit("/", 1)[1])
    return parsed[1] if parsed else None


def version_cell(build, platforms):
    """Shows a build like 42.40-CL-59343437, taking the changelist from that platform's link."""
    packages = [build["packages"][key] for key in platforms if key in build["packages"]]
    if not packages:
        return ""
    marker = " *" if build.get("inferred") else ""
    changelist = link_changelist(packages[0])
    if not changelist:
        return build["build"] + marker
    version = build["build"].rsplit("-CL-", 1)[0]
    return f"{version}-CL-{changelist}{marker}"


def build_row(build):
    # A row with no links is still a known Xbox build, so it keeps its name in the Xbox column.
    name = build["build"] + (" *" if build.get("inferred") else "")
    xbox = version_cell(build, ("xbox_one", "xbox_series")) or ("" if build["packages"] else name)
    pc = version_cell(build, ("pc",))
    links = [package_link(build["packages"].get(key)) for key in PLATFORMS]
    cells = [xbox, pc, build.get("date") or "", *links, build.get("notes") or ""]
    return "| " + " | ".join(cells) + " |"


def build_table(builds):
    lines = ["| " + " | ".join(HEADERS) + " |", "|" + "---|" * len(HEADERS)]
    lines.extend(build_row(build) for build in builds)
    return "\n".join(lines)


def live_section(live):
    if not live:
        return ""
    lines = ["## Live now"]
    if live.get("version"):
        lines.append(f"- **Game version:** {live['version']} (server CL {live['server_cl']})")
    sizes = [
        f"{label} {size_text(live['store_sizes'][key])}"
        for key, label in PLATFORMS.items()
        if live.get("store_sizes", {}).get(key)
    ]
    if sizes:
        lines.append(f"- **Store download size:** {', '.join(sizes)}")
    return "\n".join(lines) + "\n"


def season_title(season):
    return "Pre-Battle Royale" if season == 0 else f"Season {season}"


def render(db):
    parts = [INTRO, live_section(db.get("live")), LEGEND]
    newest_first = sorted(db["builds"], key=lambda build: (build["season"], build["cl"] or 0), reverse=True)
    for season, group in groupby(newest_first, key=lambda build: build["season"]):
        parts.append(f"# {season_title(season)}\n{build_table(group)}\n")

    parts.append(CREDITS)
    return "\n".join(part for part in parts if part)


def write_readme(db):
    README_FILE.write_text(render(db), encoding="utf-8", newline="\n")


def parse_size(text):
    match = SIZE.fullmatch(text)
    return round(float(match[1]) * SIZE_UNITS[match[2]]) if match else None


def parse_package(cell):
    match = LINK.fullmatch(cell)
    if not match:
        return None
    label, url = match.groups()
    return {"url": url, "size": parse_size(label), "alive": label != "removed"}


def parse_row(line, season):
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    xbox, pc, date, *links, notes = cells
    name = xbox or pc
    build = {
        "build": name.removesuffix(" *"),
        "cl": int(CHANGELIST.search(name)[1]),
        "season": season,
        "date": date or None,
        "packages": {},
        "notes": notes,
    }
    for platform, cell in zip(PLATFORMS, links):
        package = parse_package(cell)
        if package:
            build["packages"][platform] = package
    if name.endswith(" *"):
        build["inferred"] = True
    return build


def parse_live_sizes(line):
    sizes = {}
    for key, label in PLATFORMS.items():
        match = re.search(rf"{label} ([\d.]+ [GM]B)", line)
        if match:
            sizes[key] = parse_size(match[1])
    return sizes


def read_readme():
    """Reads back everything render() wrote."""
    db = {"live": {}, "builds": []}
    season = None
    for line in README_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            heading = SEASON_HEADING.fullmatch(line)
            season = int(heading[1] or 0) if heading else None
        elif season is not None and line.startswith("| ") and not line.startswith("| Xbox Version"):
            db["builds"].append(parse_row(line, season))
        elif version := LIVE_VERSION.search(line):
            db["live"].update(version=version[1], server_cl=int(version[2]))
        elif line.startswith("- **Store download size:**"):
            db["live"]["store_sizes"] = parse_live_sizes(line)
    return db
