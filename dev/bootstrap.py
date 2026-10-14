"""Onboard the dev Home Assistant (user dev/dev) and write dev/.env with a token.

Safe to re-run: logs in instead if the instance is already onboarded.
"""

import asyncio
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import niquests

BASE = "http://homeassistant.local:8123"
CLIENT_ID = f"{BASE}/"
USER, PASSWORD = "dev", "dev"
ENV_FILE = Path(__file__).parent / ".env"


def request(path, data=None, token=None, form=False):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, body, headers)
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - fixed local dev HA URL
        return json.loads(resp.read() or b"null")


def wait_for_ha(timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request("/api/onboarding")
            return
        except urllib.error.HTTPError:
            return  # responding (onboarding finished => 404/401 is fine)
        except urllib.error.URLError, ConnectionError, TimeoutError:
            time.sleep(2)
    sys.exit("Home Assistant did not come up on " + BASE)


def auth_code():
    try:
        result = request(
            "/api/onboarding/users",
            {
                "client_id": CLIENT_ID,
                "name": "Dev",
                "username": USER,
                "password": PASSWORD,
                "language": "en",
            },
        )
        return result["auth_code"], True
    except urllib.error.HTTPError as err:
        if err.code != 403:  # 403: user step already done
            raise
    flow = request(
        "/auth/login_flow",
        {
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
    )
    result = request(
        f"/auth/login_flow/{flow['flow_id']}",
        {"client_id": CLIENT_ID, "username": USER, "password": PASSWORD},
    )
    if result.get("type") != "create_entry":
        sys.exit(f"Login failed: {result}")
    return result["result"], False


def finish_onboarding(token):
    for step, data in [
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
    ]:
        try:
            request(f"/api/onboarding/{step}", data, token)
        except urllib.error.HTTPError:
            pass  # step already done


async def long_lived_token(access_token):
    async with niquests.AsyncSession() as session:
        resp = await session.get(BASE.replace("http", "ws") + "/api/websocket")
        ws = resp.extension
        if ws is None:
            raise OSError("Unable to use websockets")
        await ws.next_payload()
        await ws.send_payload(
            json.dumps({"type": "auth", "access_token": access_token})
        )
        await ws.next_payload()
        await ws.send_payload(
            json.dumps({
                "id": 1,
                "type": "auth/long_lived_access_token",
                "client_name": f"ha-repl-dev-{int(time.time())}",
                "lifespan": 3650,
            })
        )
        msg = json.loads(await ws.next_payload())
        if not msg["success"]:
            sys.exit(f"Could not create token: {msg}")
        return msg["result"]


def main():
    wait_for_ha()
    code, new = auth_code()
    tokens = request(
        "/auth/token",
        {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
        form=True,
    )
    if new:
        finish_onboarding(tokens["access_token"])
    token = asyncio.run(long_lived_token(tokens["access_token"]))
    ENV_FILE.write_text(f"HASS_SERVER={BASE}\nHASS_TOKEN={token}\n")
    print(f"Wrote {ENV_FILE} (login user: {USER} at {BASE})")


if __name__ == "__main__":
    main()
