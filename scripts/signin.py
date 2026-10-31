"""One-time sign-in. Saves the refresh token that update.py and GitHub Actions use."""
import sys
from pathlib import Path

import xbl

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".xbox-refresh-token"


def main():
    try:
        login = xbl.start_device_login()
        print(f"\n1. Open {login['verification_uri']}")
        print(f"2. Enter this code: {login['user_code']}")
        print("3. Sign in with the Microsoft account you want the tracker to use.\n")
        print("Waiting for you to finish signing in...")

        tokens = xbl.wait_for_device_login(login)
        xbl.update_auth_header(tokens["access_token"])
    except xbl.AuthError as error:
        print(f"\nThat didn't work: {error}")
        return 1

    TOKEN_FILE.write_text(tokens["refresh_token"], encoding="utf-8")
    print(f"\nSigned in. Token saved to {TOKEN_FILE.name}, which git ignores.")
    print("Treat that file like a password and never commit or share it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
