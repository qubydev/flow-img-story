"""Google Flow image transport using a user's own persistent Chrome profile.

Flow's web RPC is undocumented and can change. All browser authentication stays
in the local profile; this module never asks for a Google password.
"""

import json
import os
import random
import re
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ORIGIN = "https://flow.google.com"
# Start at Flow itself so Google can return to the product that we need to
# verify.  The old google.com callback was too strict: Google may leave the
# tab on flow.google.com after sign-in.
GOOGLE_SIGN_IN = f"{ORIGIN}/"
SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
ASPECTS = {"1:1": 1, "9:16": 2, "16:9": 3, "3:4": 4, "4:3": 5}
MODELS = {"Nano Banana 2 Lite": "NARWHAL", "Nano Banana Pro": "GEM_PIX_2", "Imagen (Harbor Seal)": "HARBOR_SEAL"}


def image_payload(project, token, prompt, model, aspect, seed):
    context = [None, 22, None, None, None, project, None, None, None, None, [token, None]]
    # Sparse positional shape recorded in original/ phase-0 image capture.
    return [None, [[None, None, None, seed, ASPECTS[aspect], MODELS[model], None,
                    context, [[prompt]], str(uuid.uuid4()).upper()]]]


def parse_rpc(body, rpcid):
    """Unwrap Google's XSSI-prefixed batchexecute response."""
    for line in body.splitlines():
        if not line.startswith("["):
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in rows:
            if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and row[1] == rpcid:
                return json.loads(row[2]) if isinstance(row[2], str) else row[2]
    raise RuntimeError(f"Flow returned no {rpcid} result. Check the account and model.")


def _find_image(value):
    if isinstance(value, list):
        if len(value) > 2 and isinstance(value[0], str) and isinstance(value[2], str):
            urls = list(_urls(value))
            if urls:
                return value[0], urls[0]
        for child in value:
            found = _find_image(child)
            if found:
                return found
    return None


def _urls(value):
    if isinstance(value, str) and value.startswith("https://flow-content.google/image/"):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _urls(child)


class FlowClient:
    def __init__(self, profile):
        self.profile = Path(profile)

    def _browser(self, playwright, headless=False):
        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            return playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=headless,
                chromium_sandbox=True,
                ignore_default_args=["--enable-automation"],
                accept_downloads=True, viewport={"width": 1280, "height": 900})
        except Exception as exc:
            raise RuntimeError("Google Chrome is required. Close any other window using this account profile.") from exc

    def _chrome_path(self):
        locations = [
            shutil.which("chrome"),
            Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        for location in locations:
            if location and Path(location).is_file():
                return str(location)
        raise RuntimeError("Google Chrome is not installed.")

    def sign_in(self, progress=None):
        from playwright.sync_api import sync_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        # Start ordinary Chrome. Playwright does not launch or control the
        # Google sign-in page; it attaches only after a real page is available.
        process = subprocess.Popen([
            self._chrome_path(), f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={port}", "--new-window",
            "--no-first-run", "--no-default-browser-check", GOOGLE_SIGN_IN,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        endpoint = f"http://127.0.0.1:{port}"
        browser = None
        try:
            with sync_playwright() as playwright:
                for _ in range(300):
                    try:
                        with urlopen(endpoint + "/json/list", timeout=2) as reply:
                            targets = json.load(reply)
                    except (URLError, TimeoutError, OSError, ValueError):
                        if process.poll() is not None:
                            raise RuntimeError("Chrome closed before sign-in completed.")
                        time.sleep(1)
                        continue
                    urls = [target.get("url", "") for target in targets if target.get("type") == "page"]
                    if any("/signin/rejected" in url for url in urls):
                        raise RuntimeError("Google rejected sign-in in Chrome. The account was not verified.")
                    # Authentication can finish on Flow, Google home, or a
                    # Google account page that has redirected back to Flow.
                    authenticated_host = any(
                        re.match(r"https://([\w-]+\.)?flow\.google\.com(/|$)", url)
                        or re.match(r"https://(www\.)?google\.com(/|$)", url)
                        for url in urls
                    )
                    if authenticated_host:
                        if browser is None:
                            browser = playwright.chromium.connect_over_cdp(endpoint)
                        context = browser.contexts[0]
                        pages = context.pages
                        home = next((page for page in pages if
                            re.match(r"https://([\w-]+\.)?flow\.google\.com(/|$)", page.url)
                            or re.match(r"https://(www\.)?google\.com(/|$)", page.url)), None)
                        if home:
                            cookies = context.cookies([
                                "https://flow.google.com", "https://accounts.google.com",
                                "https://www.google.com",
                            ])
                            signed = any(
                                c.get("name") in ("SID", "__Secure-3PSID", "SAPISID", "__Secure-1PSID")
                                and c.get("value") for c in cookies
                            )
                            # Do not depend on an avatar/aria-label.  Those
                            # labels vary by locale and by the current Flow UI.
                            if signed and progress:
                                progress("Checking Flow access…")
                            if signed:
                                response = context.request.get(ORIGIN, timeout=60000)
                                if not response.ok or "accounts.google.com" in response.url:
                                    raise RuntimeError("Google sign-in succeeded, but Flow access could not be verified.")
                                if not re.search(r'"SNlM0e"\s*:\s*"[^"\\]+', response.text()):
                                    raise RuntimeError("Google sign-in succeeded, but Flow did not return an active session.")
                                email = None
                                for label in home.locator(
                                    '[aria-label*="Google Account"], [aria-label*="Google konto"], '
                                    'a[href*="SignOutOptions"]'
                                ).all()[:3]:
                                    match = re.search(
                                        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                                        label.get_attribute("aria-label") or "", re.I)
                                    if match:
                                        email = match.group(0)
                                        break
                                return email
                    time.sleep(1)
                raise RuntimeError("Google sign-in was not completed within five minutes.")
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()

    def generate(self, prompt, model, aspect, count, seed, output):
        from playwright.sync_api import sync_playwright

        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        paths = []
        with sync_playwright() as playwright:
            browser = self._browser(playwright)
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                page.goto(ORIGIN, wait_until="domcontentloaded", timeout=60000)
                if "accounts.google.com" in page.url:
                    raise RuntimeError("Flow account is signed out. Use Add / sign in again.")
                html = page.content()
                match = re.search(r'"SNlM0e"\s*:\s*"([^"\\]+)', html)
                if not match:
                    raise RuntimeError("Could not read the Flow session token. Sign in again or check Flow access.")
                xsrf = match.group(1)
                auth_match = re.search(r"/u/(\d+)/", page.url)
                auth_index = auth_match.group(1) if auth_match else "0"
                endpoint = f"{ORIGIN}/u/{auth_index}/_/AiSandboxAngularFrontend/data/batchexecute"

                def call(rpcid, args):
                    form = {"f.req": json.dumps([[[rpcid, json.dumps(args, separators=(',', ':')), None, "generic"]]], separators=(',', ':')),
                            "at": xsrf}
                    response = browser.request.post(endpoint, form=form, headers={"Origin": ORIGIN,
                        "Referer": f"{ORIGIN}/u/{auth_index}/", "X-Same-Domain": "1"}, timeout=120000)
                    if not response.ok:
                        raise RuntimeError(f"Flow {rpcid} returned HTTP {response.status}")
                    return parse_rpc(response.text(), rpcid)

                created = call("jHPbke", ["projects/*", [None, ["PINHOLE"]], [None, 22]])
                project = created[0]
                if not isinstance(project, str):
                    raise RuntimeError("Flow did not create a project.")
                page.goto(f"{ORIGIN}/about", wait_until="domcontentloaded", timeout=60000)
                page.add_script_tag(url=f"https://www.google.com/recaptcha/enterprise.js?render={SITE_KEY}")
                page.wait_for_function("window.grecaptcha && window.grecaptcha.enterprise", timeout=30000)
                for index in range(count):
                    token = page.evaluate("key => new Promise((resolve, reject) => { grecaptcha.enterprise.ready(() => grecaptcha.enterprise.execute(key, {action: 'IMAGE_GENERATION'}).then(resolve, reject)); })", SITE_KEY)
                    result = call("ogiZ0b", image_payload(project, token, prompt, model, aspect,
                                                          seed if seed is not None else random.randint(1, 2**31 - 1)))
                    found = _find_image(result)
                    if not found:
                        raise RuntimeError("Flow returned no image URL for this prompt.")
                    media_id, url = found
                    response = browser.request.get(url, timeout=120000)
                    if not response.ok:
                        raise RuntimeError(f"Image download returned HTTP {response.status}")
                    content_type = response.headers.get("content-type", "")
                    suffix = ".png" if "png" in content_type else ".jpg"
                    path = output / f"{media_id}_{index + 1}{suffix}"
                    path.write_bytes(response.body())
                    paths.append(str(path))
            finally:
                browser.close()
        return paths
