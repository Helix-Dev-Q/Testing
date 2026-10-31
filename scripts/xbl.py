"""Xbox Live sign-in and package lookups."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# The ID that LukeFZ/MsixvcPackageDownloader signs in with. It belongs to Mojang, not this
# project, so set XBOX_CLIENT_ID if you have your own registered app.
CLIENT_ID = os.environ.get("XBOX_CLIENT_ID") or "00000000402b5328"
SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
USER_AGENT = "FortniteXboxBuilds/1.0"
UPDATE_FILE = re.compile(r"update-(\d+\.\d+\.\d+\.\d+\.[0-9a-fA-F-]{36})\.xsp")

XSTS_ERRORS = {
    2148916233: "this Microsoft account has no Xbox profile yet. Sign in at https://www.xbox.com once to create one.",
    2148916235: "Xbox Live isn't available in this account's country.",
    2148916236: "this account needs adult verification.",
    2148916237: "this account needs adult verification.",
    2148916238: "this is a child account. Use an adult account or add it to a family.",
}


class AuthError(Exception):
    """Signing in failed in a way that needs a person to fix it."""


class PackageDenied(Exception):
    """The package API refused one package. Others may still work."""


def _post(url, body, headers):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return json.load(error) | {"_status": error.code}
        except ValueError:
            return {"_status": error.code}


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    return _post(url, body, {"Content-Type": "application/x-www-form-urlencoded"})


def _post_json(url, payload):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-xbl-contract-version": "1",
    }
    return _post(url, json.dumps(payload).encode(), headers)


def start_device_login():
    response = _post_form(
        "https://login.live.com/oauth20_connect.srf",
        {"client_id": CLIENT_ID, "scope": SCOPE, "response_type": "device_code"},
    )
    if "device_code" not in response:
        raise AuthError(f"Microsoft refused the sign-in request: {response}")
    return response


def wait_for_device_login(login):
    deadline = time.monotonic() + int(login.get("expires_in", 900))
    interval = int(login.get("interval", 5))
    while time.monotonic() < deadline:
        time.sleep(interval)
        response = _post_form(
            "https://login.live.com/oauth20_token.srf",
            {"grant_type": DEVICE_GRANT, "client_id": CLIENT_ID, "device_code": login["device_code"]},
        )
        if "access_token" in response:
            return response
        error = response.get("error")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            raise AuthError(f"Sign-in failed: {error or response}")
    raise AuthError("Sign-in timed out. Run it again.")


def refresh(refresh_token):
    response = _post_form(
        "https://login.live.com/oauth20_token.srf",
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "refresh_token": refresh_token,
        },
    )
    if "access_token" not in response:
        raise AuthError(
            f"The saved sign-in no longer works ({response.get('error', 'unknown error')}). "
            "Run scripts/signin.py again and update the XBOX_REFRESH_TOKEN secret."
        )
    return response


def update_auth_header(access_token):
    """Turns a Microsoft access token into the header the package API expects."""
    user = _post_json(
        "https://user.auth.xboxlive.com/user/authenticate",
        {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"t={access_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        },
    )
    if "Token" not in user:
        raise AuthError(f"Xbox Live refused the user token: {user.get('_status')} {user.get('XErr', '')}")

    xsts = _post_json(
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [user["Token"]]},
            "RelyingParty": "http://update.xboxlive.com",
            "TokenType": "JWT",
        },
    )
    if "Token" not in xsts:
        code = xsts.get("XErr")
        reason = XSTS_ERRORS.get(code, f"error {code or xsts.get('_status')}")
        raise AuthError(f"Xbox Live refused access: {reason}")

    user_hash = xsts["DisplayClaims"]["xui"][0]["uhs"]
    return f"XBL3.0 x={user_hash};{xsts['Token']}"


def _get_package(url, auth_header):
    request = urllib.request.Request(
        url,
        headers={"Authorization": auth_header, "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            package = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise AuthError(f"The package API rejected the token ({error.code}).")
        if error.code == 403:
            reason = error.headers.get("X-Err") or error.read(300).decode("utf-8", "replace")
            raise PackageDenied(f"403 {reason}".strip())
        if error.code == 404:
            return None
        raise
    return package if package.get("PackageFound", True) else None


def get_base_package(content_id, auth_header):
    """Returns the newest package for a content ID, or None if there isn't one."""
    return _get_package(f"https://packagespc.xboxlive.com/GetBasePackage/{content_id}", auth_header)


def get_specific_package(content_id, version_id, auth_header):
    """Returns one exact version of a package, or None if Xbox doesn't have it."""
    url = f"https://packagespc.xboxlive.com/GetSpecificBasePackage/{content_id}/{version_id}"
    return _get_package(url, auth_header)


def older_version_ids(package):
    """Lists the older versions a package has update files for, using the IDs in the file names."""
    names = (entry.get("FileName") or "" for entry in package.get("PackageFiles") or [])
    matches = (UPDATE_FILE.fullmatch(name) for name in names)
    return sorted({match[1] for match in matches if match})


def package_files(package):
    """Lists the full packages in a response as (file name, size, url), skipping updates and hashes."""
    files = []
    for entry in package.get("PackageFiles") or []:
        name = entry.get("FileName") or ""
        roots = entry.get("CdnRootPaths") or []
        if not roots or name.lower().endswith((".phf", ".xsp")):
            continue
        root = next((r for r in roots if "assets1.xboxlive.com" in r), roots[0])
        url = root.rstrip("/") + "/" + entry["RelativeUrl"].lstrip("/")
        files.append((name, entry.get("FileSize"), url))
    return files
