"""
tests/test_assets.py
The page shell and its static files:

- nothing is loaded from the internet (scripts, styles and fonts are served
  by the app, so a kiosk without internet still works)
- every referenced file exists
- the asset version only changes when a file changes
- the sidebar, status bar and page headings are present
- the interface rules in notes/DESIGN.md hold for every file under web/
- colour tokens meet contrast targets in light and dark mode, for every theme

Plain script, no pytest needed.
"""

import colorsys
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
WEB = os.path.join(PROJECT_ROOT, "web")
STATIC = os.path.join(WEB, "static")


def get_real_cfg_hash():
    p = os.path.join(PROJECT_ROOT, "config.json")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return None


_cfg_hash_before = get_real_cfg_hash()

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


def web_files(exts):
    for folder, _, files in os.walk(WEB):
        for name in files:
            if name.lower().endswith(exts):
                yield os.path.join(folder, name)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# Our own code; the vendor files are third-party and are not held to our rules
OWN_FILES = [p for p in web_files((".html", ".css", ".js")) if os.sep + "vendor" + os.sep not in p]
CSS = read(os.path.join(STATIC, "css", "style.css"))

client = server.app.test_client()
res = client.get("/")
html = res.get_data(as_text=True)

# ──────────────────────────────────────────────────────────────────────────
section("1. Nothing comes from the internet")

check("Page loads", res.status_code == 200)
external = re.findall(r'(?:src|href)\s*=\s*"((?:https?:)?//[^"]+)"', html)
check("No external script, style or font links in the page", external == [], str(external))
check("No external url() in the stylesheet", not re.search(r"url\(\s*['\"]?(?:https?:)?//", CSS))
check("No @import in the stylesheet", "@import" not in CSS)

# ──────────────────────────────────────────────────────────────────────────
section("2. Every referenced file exists")

refs = sorted(set(re.findall(r'"/static/([^"?]+)(?:\?[^"]*)?"', html)))
missing = [r for r in refs if not os.path.isfile(os.path.join(STATIC, *r.split("/")))]
check(f"All {len(refs)} files referenced by the page exist", not missing, str(missing))

css_refs = sorted(set(re.findall(r"url\(\s*'?\.\./([^')]+)'?\s*\)", CSS)))
css_missing = [r for r in css_refs if not os.path.isfile(os.path.join(STATIC, *r.split("/")))]
check(f"All {len(css_refs)} font files referenced by the stylesheet exist", css_refs and not css_missing, str(css_missing))

for rel, marker in (("vendor/socket.io.min.js", "Socket.IO v4.7.5"), ("vendor/chart.umd.min.js", "chart.js")):
    path = os.path.join(STATIC, *rel.split("/"))
    content = read(path) if os.path.isfile(path) else ""
    check(f"{rel} is the expected library", marker.lower() in content[:400].lower(), content[:80])

check("Font licence is included", os.path.isfile(os.path.join(STATIC, "fonts", "OFL.txt")))
check("Icon licence is included", os.path.isfile(os.path.join(STATIC, "icons", "LICENSE.txt")))

# ──────────────────────────────────────────────────────────────────────────
section("3. Asset version")

versions = set(re.findall(r"\?v=([0-9a-f]+)", html))
check("The page uses one asset version", len(versions) == 1, str(versions))
second = set(re.findall(r"\?v=([0-9a-f]+)", client.get("/").get_data(as_text=True)))
check("The version is the same on the next request", versions == second, f"{versions} vs {second}")
check("The version is the fingerprint of web/", versions == {server.static_fingerprint(WEB)})

copy_root = os.path.join(tmp_dir, "web_copy")
shutil.copytree(WEB, copy_root)
before = server.static_fingerprint(copy_root)
check("Fingerprint is stable when nothing changes", server.static_fingerprint(copy_root) == before)
with open(os.path.join(copy_root, "static", "css", "style.css"), "a", encoding="utf-8") as f:
    f.write("\n/* changed */\n")
check("Fingerprint changes when a file changes", server.static_fingerprint(copy_root) != before)

# ──────────────────────────────────────────────────────────────────────────
section("4. Shell structure")

tabs = re.findall(r'<button class="tab-btn[^"]*" data-tab="(\w+)"', html)
check("Sidebar has the five sections in order",
      tabs == ["monitor", "attendance", "dashboard", "register", "settings"], str(tabs))
check("Sidebar is a vertical tab list", 'aria-orientation="vertical"' in html)
for item in ("sbCamera", "sbDatabase", "sbSocket", "sbUpdated"):
    check(f"Status bar has #{item}", f'id="{item}"' in html)
for panel in ("monitor", "attendance", "dashboard", "register", "settings"):
    check(f"Panel {panel} has its own heading", f'<h2 class="panel-title" id="title-{panel}">' in html)
check("Event list has Pause and Clear", 'id="btnFeedPause"' in html and 'id="btnFeedClear"' in html)
check("Dark mode is the default before first paint", 'var mode = "dark";' in html)

# ──────────────────────────────────────────────────────────────────────────
section("5. Interface rules (notes/DESIGN.md)")

dash_hits = [os.path.relpath(p, PROJECT_ROOT) for p in OWN_FILES if re.search("[—–]", read(p))]
check("Rule 6: no em or en dashes", not dash_hits, str(dash_hits))

gradient_hits = [os.path.relpath(p, PROJECT_ROOT) for p in OWN_FILES if "gradient" in read(p).lower()]
check("Rule 1: no gradients", not gradient_hits, str(gradient_hits))


def is_purple(hex_value):
    r, g, b = (int(hex_value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return 255 <= h * 360 <= 300 and s > 0.25 and 0.1 < l < 0.95


purple = sorted({m for p in OWN_FILES for m in re.findall(r"#([0-9a-fA-F]{6})\b", read(p)) if is_purple(m)})
check("Rule 1: no purple or violet colours", not purple, str(purple))

bad_radius = []
for value in re.findall(r"border-radius:\s*([^;]+);", CSS):
    for part in value.split():
        if part.startswith("var(--radius") or part in ("0", "50%"):
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)px", part)
        if not m or float(m.group(1)) > 6:
            bad_radius.append(value)
check("Rule 2: corner radius never above 6px (round dots excepted)", not bad_radius, str(bad_radius))
radius_tokens = dict(re.findall(r"--(radius(?:-sm)?):\s*(\d+)px", CSS))
check("Rule 2: radius tokens are 2 to 6px", radius_tokens and all(2 <= int(v) <= 6 for v in radius_tokens.values()),
      str(radius_tokens))

EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
emoji_hits = [os.path.relpath(p, PROJECT_ROOT) for p in OWN_FILES if EMOJI.search(read(p))]
check("Rule 5: no emoji", not emoji_hits, str(emoji_hits))

keyframes = sorted(set(re.findall(r"@keyframes\s+([\w-]+)", CSS)))
check("Rule 7: only the allowed animations (live indicator, new row)",
      keyframes == ["livePulse", "rowNew"], str(keyframes))
js_animations = [os.path.relpath(p, PROJECT_ROOT) for p in OWN_FILES
                 if p.endswith(".js") and re.search(r"\.style\.animation\s*=|requestAnimationFrame", read(p))]
check("Rule 7: no animation started from JavaScript", not js_animations, str(js_animations))
check("Rule 7: reduced motion switches animation off",
      re.search(r"prefers-reduced-motion: reduce\)\s*\{[^}]*animation: none !important", CSS) is not None)
check("Rule 7: no custom cursor", not re.search(r"cursor:\s*url\(", CSS))

# ──────────────────────────────────────────────────────────────────────────
section("6. Colour contrast")


def block(selector):
    start = CSS.index(selector + " {")
    return CSS[start:CSS.index("\n}", start)]


def tokens(text):
    return {k: v.lower() for k, v in re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", text)}


def lum(hex_value):
    def channel(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    h = hex_value.lstrip("#")
    r, g, b = channel(h[0:2]), channel(h[2:4]), channel(h[4:6])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


light = tokens(block(":root"))
dark = dict(light)
dark.update(tokens(block(':root[data-mode="dark"]')))
THEMES = ["orange", "ink", "teal", "blue", "navy", "olive", "coffee", "steel"]

for mode, t in (("light", light), ("dark", dark)):
    problems = []
    for bg in ("bg", "surface", "surface-2"):
        for fg, need in (("text", 4.5), ("text-dim", 4.5), ("border-strong", 3.0)):
            if ratio(t[fg], t[bg]) < need:
                problems.append(f"{fg} on {bg} {ratio(t[fg], t[bg]):.2f}")
    for status in ("success", "warning", "danger", "info"):
        for bg in ("surface", status + "-soft"):
            if ratio(t[status], t[bg]) < 4.5:
                problems.append(f"{status} on {bg} {ratio(t[status], t[bg]):.2f}")
    if ratio(t["danger-contrast"], t["danger"]) < 4.5:
        problems.append("danger button text")
    for theme in THEMES:
        acc, contrast, soft = t["t-" + theme], t["t-%s-contrast" % theme], t["t-%s-soft" % theme]
        for label, a, b, need in (("accent on surface", acc, t["surface"], 4.5),
                                  ("button text", contrast, acc, 4.5),
                                  ("text on soft accent", t["text"], soft, 4.5)):
            if ratio(a, b) < need:
                problems.append(f"{theme} {label} {ratio(a, b):.2f}")
    check(f"{mode} mode: every text, border, status and theme pair meets its target", not problems, str(problems))

# ──────────────────────────────────────────────────────────────────────────
section("7. Real config.json untouched")

check("config.json hash unchanged", get_real_cfg_hash() == _cfg_hash_before)

shutil.rmtree(tmp_dir, ignore_errors=True)

print(f"\n{'=' * 60}")
print(f"  RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
