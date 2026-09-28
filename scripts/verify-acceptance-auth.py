"""Read-only PKCE and API authorization smoke check for the local acceptance realm."""

import base64
import hashlib
import json
import secrets
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = "http://127.0.0.1:18080/realms/invoice-acceptance"
CLIENT_ID = "invoice-intelligence-console"
REDIRECT_URI = "http://127.0.0.1:15173/"
API = "http://127.0.0.1:18000"


class LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form" and dict(attrs).get("id") == "kc-form-login":
            self.action = dict(attrs).get("action")


def token_for(username: str, password: str) -> str:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        login = client.get(
            f"{AUTHORITY}/protocol/openid-connect/auth",
            params={
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": "openid profile",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        login.raise_for_status()
        form = LoginForm()
        form.feed(login.text)
        if form.action is None:
            raise RuntimeError(f"{username}: Keycloak login form is missing")
        # Loopback HTTP is a secure context in browsers; httpx does not send Secure cookies over it.
        for cookie in client.cookies.jar:
            cookie.secure = False
        result = client.post(
            urljoin(str(login.url), form.action), data={"username": username, "password": password}
        )
        if result.status_code != 302:
            text = result.text.lower()
            detail = (
                "invalid credentials"
                if "invalid username or password" in text
                else "required action"
                if "update password" in text
                else "form rejected"
            )
            raise RuntimeError(
                f"{username}: login returned {result.status_code} ({detail}; "
                f"cookie_names={','.join(client.cookies.keys())}; "
                f"request_has_cookie={'cookie' in result.request.headers}; "
                f"form_host={urlparse(str(result.request.url)).hostname})"
            )
        location = result.headers.get("location", "")
        if not location.startswith(REDIRECT_URI):
            target = urlparse(location)
            raise RuntimeError(
                f"{username}: login redirect host={target.hostname}, path={target.path}"
            )
        query = parse_qs(urlparse(location).query)
        if query.get("state") != [state] or not query.get("code"):
            raise RuntimeError(f"{username}: authorization code/state is missing")
        token_response = client.post(
            f"{AUTHORITY}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": query["code"][0],
                "code_verifier": verifier,
            },
        )
        if token_response.status_code != 200:
            raise RuntimeError(f"{username}: token exchange returned {token_response.status_code}")
        return str(token_response.json()["access_token"])


def main() -> None:
    path = ROOT / "secrets" / "acceptance-users.txt"
    credentials = json.loads(path.read_text(encoding="utf-8"))
    for key, allowed, denied in (
        ("reviewer", "/api/v1/reviews", "/api/v1/memory/admissions?limit=1"),
        ("governor", "/api/v1/memory/admissions?limit=1", "/api/v1/reviews"),
    ):
        account = credentials[key]
        token = token_for(account["username"], account["password"])
        signing_key = (
            jwt.PyJWKClient(f"{AUTHORITY}/protocol/openid-connect/certs")
            .get_signing_key_from_jwt(token)
            .key
        )
        try:
            jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                audience="invoice-intelligence-api",
                issuer=AUTHORITY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise RuntimeError(
                f"{account['username']}: JWT validation {type(exc).__name__}"
            ) from None
        with httpx.Client(
            base_url=API, timeout=10, headers={"Authorization": f"Bearer {token}"}
        ) as client:
            allowed_status = client.get(allowed).status_code
            denied_status = client.get(denied).status_code
        if allowed_status != 200 or denied_status != 403:
            raise RuntimeError(
                f"{account['username']}: allowed={allowed_status}, denied={denied_status}"
            )
        print(f"{account['username']}: OIDC PKCE 与 API 角色隔离通过")


if __name__ == "__main__":
    main()
