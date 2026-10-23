"""
tests/test_preferences.py
Per-account appearance: mode, colour theme and font. Saved to the account,
applied before first paint, and the font choices are all served by the app.

Uses an isolated temp config. Never touches the real config.json or instance/.

Run: python tests/test_preferences.py
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def get_real_cfg_hash():
    p = os.path.join(PROJECT_ROOT, "config.json")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


_cfg_hash_before = get_real_cfg_hash()

# Setup isolated temp config
tmp_dir = tempfile.mkdtemp(prefix="face_web_test_")
tmp_cfg_path = os.path.join(tmp_dir, "config.json")
real_cfg_path = os.path.join(PROJECT_ROOT, "config.json")
if os.path.exists(real_cfg_path):
    with open(real_cfg_path, "r", encoding="utf-8") as f:
        cfg_copy = json.load(f)
else:
    from core.model_loader import DEFAULT_CONFIG
    cfg_copy = dict(DEFAULT_CONFIG)

cfg_copy["mongodb_db"] = "face_attendance_test"
with open(tmp_cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg_copy, f, indent=2)

os.environ["FACE_WEB_CONFIG"] = tmp_cfg_path

import server
from auth.store import DEFAULT_PREFS, FONTS, MODES, THEMES
from helpers import sign_in, signed_in_client

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  -- {detail}")


def section(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def read(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def prepaint_prefs(html):
    """The prefs object the server wrote into the pre-paint script."""
    match = re.search(r"var prefs = (\{.*?\});", html)
    return json.loads(match.group(1)) if match else None


client = signed_in_client(server)

# ──────────────────────────────────────────────────────────────────────────
section("1. Defaults")

check("Default is dark, burnt orange, IBM Plex Sans",
      DEFAULT_PREFS == {"mode": "dark", "theme": "orange", "font": "plex"})
res = client.get("/api/me")
check("/api/me returns the defaults for a new account", res.get_json().get("prefs") == DEFAULT_PREFS,
      str(res.get_json()))
html = client.get("/").get_data(as_text=True)
check("Page writes the account's choices into the pre-paint script", prepaint_prefs(html) == DEFAULT_PREFS,
      str(prepaint_prefs(html)))
check("Pre-paint script sets the font", "root.dataset.font" in html)

# ──────────────────────────────────────────────────────────────────────────
section("2. Saving")

res = client.put("/api/me/preferences", json={"mode": "light"})
check("PUT one preference -> 200", res.status_code == 200 and res.get_json().get("ok") is True)
check("Response returns the merged preferences",
      res.get_json().get("prefs") == {"mode": "light", "theme": "orange", "font": "plex"}, str(res.get_json()))

res = client.put("/api/me/preferences", json={"theme": "teal", "font": "atkinson"})
check("PUT two at once -> 200", res.status_code == 200)
expected = {"mode": "light", "theme": "teal", "font": "atkinson"}
check("Saved to the account", client.get("/api/me").get_json()["prefs"] == expected)
html = client.get("/").get_data(as_text=True)
check("Next page load paints the saved choices", prepaint_prefs(html) == expected, str(prepaint_prefs(html)))

for mode in MODES:
    check(f"Mode {mode} accepted", client.put("/api/me/preferences", json={"mode": mode}).status_code == 200)
for theme in THEMES:
    check(f"Theme {theme} accepted", client.put("/api/me/preferences", json={"theme": theme}).status_code == 200)
for font in FONTS:
    check(f"Font {font} accepted", client.put("/api/me/preferences", json={"font": font}).status_code == 200)

# ──────────────────────────────────────────────────────────────────────────
section("3. Refused input")

before = client.get("/api/me").get_json()["prefs"]
cases = [
    ("Unknown theme", {"theme": "purple"}),
    ("Unknown mode", {"mode": "sepia"}),
    ("Unknown font", {"font": "comic"}),
    ("Unknown key", {"wallpaper": "x"}),
    ("Good key with a bad one", {"mode": "dark", "theme": "purple"}),
    ("Empty object", {}),
    ("A list", ["mode", "dark"]),
    ("Wrong value type", {"mode": 1}),
]
for label, body in cases:
    res = client.put("/api/me/preferences", json=body)
    check(f"{label} -> 400", res.status_code == 400 and "error" in res.get_json(), str(res.get_json()))
res = client.put("/api/me/preferences", data="mode=dark", content_type="text/plain")
check("Not JSON -> 400", res.status_code == 400)
check("Nothing changed by refused requests", client.get("/api/me").get_json()["prefs"] == before)

anon = server.app.test_client()
check("Signed out -> 401", anon.put("/api/me/preferences", json={"mode": "dark"}).status_code == 401)
check("GET is not allowed", client.get("/api/me/preferences").status_code == 405)

# ──────────────────────────────────────────────────────────────────────────
section("4. Each account has its own choices")

server.users.create("second", "long-enough-2")
other = server.app.test_client()
sign_in(other, "second", "long-enough-2")
check("Second account starts with the defaults", other.get("/api/me").get_json()["prefs"] == DEFAULT_PREFS)
other.put("/api/me/preferences", json={"theme": "navy"})
check("Second account's change is its own", other.get("/api/me").get_json()["prefs"]["theme"] == "navy")
check("First account is unaffected", client.get("/api/me").get_json()["prefs"] == before)

# ──────────────────────────────────────────────────────────────────────────
section("5. Signed-out pages")

login_html = anon.get("/login").get_data(as_text=True)
check("Sign-in page has the pre-paint script", "root.dataset.font" in login_html)
check("Sign-in page writes no account choices", prepaint_prefs(login_html) == {}, str(prepaint_prefs(login_html)))
check("Signed out: falls back to this browser's last choices", 'localStorage.getItem("fa-font")' in login_html)

# ──────────────────────────────────────────────────────────────────────────
section("6. Fonts, themes and the picker")

css = read("web", "static", "css", "style.css")
families = {"plex": "IBM Plex Sans", "source": "Source Sans 3", "atkinson": "Atkinson Hyperlegible", "inter": "Inter"}
check("Every stored font has a family", set(families) == set(FONTS))
for key, family in families.items():
    check(f"{family} has @font-face rules", f"font-family: '{family}';" in css)
    if key != "plex":
        check(f"data-font={key} switches --font-sans",
              re.search(r':root\[data-font="%s"\]\s*\{\s*--font-sans: \'%s\'' % (key, re.escape(family)), css)
              is not None)
for theme in THEMES:
    if theme != "orange":
        check(f"Theme {theme} has colour rules", f'[data-theme="{theme}"]' in css)

font_files = set(re.findall(r"url\('\.\./fonts/([^']+)'\)", css))
fonts_dir = os.path.join(PROJECT_ROOT, "web", "static", "fonts")
missing = [name for name in font_files if not os.path.exists(os.path.join(fonts_dir, name))]
on_disk = {name for name in os.listdir(fonts_dir) if name.endswith(".woff2")}
check("Every font file on disk is used by the CSS", on_disk == font_files, str(on_disk ^ font_files))
check("Every font file in the CSS exists", not missing, str(missing))
for licence in ("OFL.txt", "OFL-source-sans-3.txt", "OFL-inter.txt", "OFL-atkinson-hyperlegible.txt"):
    check(f"Licence {licence} ships with the fonts",
          os.path.exists(os.path.join(PROJECT_ROOT, "web", "static", "fonts", licence)))

for font in FONTS:
    check(f"Picker has a button for {font}", f'data-font-option="{font}"' in html)
for theme in THEMES:
    check(f"Picker has a swatch for {theme}", f'data-theme-option="{theme}"' in html)

app_js = read("web", "static", "js", "app.js")
check("app.js saves choices to the account", 'fetch("/api/me/preferences"' in app_js and '"PUT"' in app_js)
check("app.js also keeps them in this browser", 'localStorage.setItem' in app_js)

# ──────────────────────────────────────────────────────────────────────────
section("7. Real config.json untouched")

check("config.json hash unchanged", get_real_cfg_hash() == _cfg_hash_before)

shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
