"""Static checks of the web UI (footypreds/web): CSP rules, escaping, API paths, structure.

No browser here (see footypreds/scripts/ui_smoke.py for that): the checks read the HTML, CSS
and JavaScript sources and compare every API path the UI calls with the FastAPI routes.
"""

import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest
from starlette.routing import Mount

from footypreds.api import create_app
from footypreds.config import PACKAGE, Settings

WEB = PACKAGE / "web"
SCRIPTS = sorted(WEB.glob("*.js"))
# Values that are data from the API or the user: interpolating them into HTML without esc()
# (or a formatter such as pct/num/money) would allow markup injection.
UNSAFE_FIELDS = {
    "home",
    "away",
    "label",
    "competition",
    "league",
    "country",
    "name",
    "reason",
    "rationale",
    "message",
    "detail",
    "summary",
    "why",
    "text",
    "title",
    "opponent",
    "score",
    "hint",
    "source",
    "category",
    "group",
    "key",
    "id",
    "match_id",
    "note",
    "notice",
    "disclaimer",
    "stage",
    "clock",
    "range",
    "type",
    "status",
    "day",
    "date",
    "kickoff",
    "created",
}
# Formatters/builders that return escaped or numeric-only markup.
SAFE_CALLS = (
    "esc(",
    "pct(",
    "num(",
    "money(",
    "signedMoney(",
    "signedPct(",
    "icon(",
    "crest(",
    "gradeBadge(",
    "statusBadge(",
    "sportTag(",
    "sportIcon(",
    "formPills(",
    "probBar(",
    "clamp01(",
    "matchHref(",
    "encodeURIComponent(",
    "kpi(",
    "chips(",
)


def sources():
    return {path.name: path.read_text(encoding="utf-8") for path in SCRIPTS}


def template_literals(source):
    """Every backtick template literal of a JS source, nested ones included (raw text)."""
    literals = []

    def skip_string(i, quote):
        i += 1
        while i < len(source) and source[i] != quote:
            i += 2 if source[i] == "\\" else 1
        return i + 1

    def skip_regex(i):
        i, in_class = i + 1, False
        while i < len(source):
            char = source[i]
            if char == "\\":
                i += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                return i + 1
            i += 1
        return i

    def code(i, closing):
        """Scan JS code from i; with closing, stop after the brace that closes ${...}."""
        depth = 0
        last = "("
        while i < len(source):
            char = source[i]
            if source.startswith("//", i):
                newline = source.find("\n", i)
                i = newline if newline != -1 else len(source)
                continue
            if source.startswith("/*", i):
                i = source.find("*/", i) + 2
                continue
            if char in "'\"":
                i = skip_string(i, char)
                last = "a"
                continue
            if char == "/" and last in "(,=:[!&|?{};+-*%<>~^":
                i = skip_regex(i)
                last = "a"
                continue
            if char == "`":
                i = template(i + 1)
                last = "a"
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                if closing and depth == 0:
                    return i + 1
                depth -= 1
            if not char.isspace():
                last = char if not (char.isalnum() or char in "_$.") else "a"
                if char == ")" or char == "]":
                    last = "a"
            i += 1
        return i

    def template(i):
        begin = i
        while i < len(source):
            char = source[i]
            if char == "\\":
                i += 2
                continue
            if char == "`":
                literals.append(source[begin:i])
                return i + 1
            if source.startswith("${", i):
                i = code(i + 2, closing=True)
                continue
            i += 1
        return i

    code(0, closing=False)
    return literals


def interpolations(literal):
    """Top-level ${...} expressions of one template literal."""
    found, i = [], 0
    while True:
        start = literal.find("${", i)
        if start == -1:
            return found
        depth, end = 1, start + 2
        while end < len(literal) and depth:
            depth += {"{": 1, "}": -1}.get(literal[end], 0)
            end += 1
        found.append(literal[start + 2 : end - 1].strip())
        i = end


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def index_tags():
    parser = Collector()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
    return parser.tags


# --- CSP: no inline styles, scripts or handlers --------------------------------------------


def test_index_has_no_inline_scripts_styles_or_handlers():
    tags = index_tags()
    for tag, attrs in tags:
        assert "style" not in attrs, tag
        assert not [a for a in attrs if a.startswith("on")], (tag, attrs)
        if tag == "script":
            assert attrs.get("src", "").startswith("./"), attrs
        if tag == "link" and attrs.get("rel") == "stylesheet":
            assert attrs["href"].startswith("./"), attrs
    assert "<style" not in (WEB / "index.html").read_text(encoding="utf-8")


def test_every_script_is_loaded_once_and_exists_and_app_js_is_last():
    scripts = [attrs["src"][2:] for tag, attrs in index_tags() if tag == "script"]
    assert sorted(scripts) == [path.name for path in SCRIPTS]
    assert len(scripts) == len(set(scripts))
    assert scripts[0] == "core.js" and scripts[-1] == "app.js"
    assert all("defer" in attrs for tag, attrs in index_tags() if tag == "script")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_scripts_have_no_inline_styles_handlers_or_eval(path):
    source = path.read_text(encoding="utf-8")
    assert source.startswith("'use strict';")
    assert not re.search(r"\sstyle\s*=\s*[\"'\\]", source), "inline style attribute"
    assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", source), "inline event handler"
    assert "setAttribute('style'" not in source and 'setAttribute("style"' not in source
    assert not re.search(r"\beval\(|new Function\(|document\.write", source)
    assert "<script" not in source
    # External resources would break the CSP (and the offline promise).
    urls = re.findall(r"https?://[^\s'\"`)]+", source)
    allowed = ("http://127.0.0.1:8000", "http://www.w3.org/2000/svg")
    assert all(u.startswith(allowed) for u in urls), urls


def test_stylesheet_has_no_external_resources():
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert not re.search(r"url\(\s*['\"]?https?:", css)
    assert "prefers-color-scheme: dark" in css


def test_no_minimum_probability_anywhere_in_the_ui():
    for name, source in sources().items():
        assert "min_probability" not in source, name
    assert "min_probability" not in (WEB / "index.html").read_text(encoding="utf-8")


# --- escaping -------------------------------------------------------------------------------


def test_template_scanner_finds_nested_interpolations():
    source = "const a = `<b>${esc(x.home)}</b>${y ? `<i>${z.label}</i>` : ''}`;"
    literals = template_literals(source)
    assert "<i>${z.label}</i>" in literals
    assert interpolations("<b>${esc(x.home)}</b>${y}") == ["esc(x.home)", "y"]


def unescaped(source):
    """Bare API string members interpolated into HTML templates without esc()."""
    unsafe = []
    for literal in template_literals(source):
        if "<" not in literal:
            continue  # not markup (URLs, plain text for textContent, toasts)
        for expression in interpolations(literal):
            if expression.startswith(SAFE_CALLS):
                continue
            # A bare data member such as `leg.home` or `m.league` must go through esc().
            bare = re.fullmatch(r"[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)+", expression)
            if bare and expression.rsplit(".", 1)[-1] in UNSAFE_FIELDS:
                unsafe.append(expression)
    return sorted(set(unsafe))


def test_escape_check_flags_raw_interpolation():
    bad = "const x = `<b>${leg.home}</b>${ok ? `<i>${m?.league}</i>` : ''}${esc(leg.away)}`;"
    assert unescaped(bad) == ["leg.home", "m?.league"]
    assert unescaped("const url = `/api/live/${item.match.id}`;") == []


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_api_strings_are_escaped_when_interpolated_into_html(path):
    unsafe = unescaped(path.read_text(encoding="utf-8"))
    assert not unsafe, f"{path.name}: interpolated without esc(): {unsafe}"


def test_escape_helper_covers_html_metacharacters():
    source = (WEB / "core.js").read_text(encoding="utf-8")
    helper = re.search(r"const esc = .*", source).group(0)
    entities = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
    for char, entity in entities.items():
        assert f"'{char}': '{entity}'" in helper or f"\"{char}\": '{entity}'" in helper, char


def test_images_use_same_origin_display_urls_with_error_fallback():
    core = (WEB / "core.js").read_text(encoding="utf-8")
    # Only /api/img?u=... style same-origin URLs reach <img src>, so CSP img-src 'self' holds.
    assert "function safeImg" in core and "url.startsWith('/')" in core
    assert "addEventListener('error'" in core
    for name, source in sources().items():
        assert "<img" not in source or name == "core.js", name


# --- API paths ------------------------------------------------------------------------------


def api_calls():
    """(method, path) of every API call in the scripts; ${...} segments become wildcards."""
    calls = set()
    pattern = re.compile(r"\b(api|post|fetch)\(\s*[`'\"](?:\$\{API_BASE\})?(/api/[^`'\"?]*)")
    href = re.compile(r"href=\"\$\{API_BASE\}(/api/[^\"?]*)")
    for source in sources().values():
        for kind, path in pattern.findall(source):
            calls.add(("POST" if kind == "post" else "GET", path))
        for path in href.findall(source):
            calls.add(("GET", path))
    return calls


def test_every_api_path_used_by_the_ui_exists(tmp_path):
    app = create_app(Settings(api_key="", database=tmp_path / "web.db"))
    routes = list(app.routes)
    first_mount = next(i for i, r in enumerate(routes) if isinstance(r, Mount))
    known = [
        (method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", route.path) + "$"))
        for route in routes[:first_mount]
        for method in getattr(route, "methods", set()) or ()
    ]
    calls = api_calls()
    assert len(calls) >= 20, calls
    missing = []
    for method, path in sorted(calls):
        concrete = re.sub(r"\$\{[^}]+\}", "x", path)
        if not any(m == method and rx.match(concrete) for m, rx in known):
            missing.append((method, path))
    assert not missing, f"UI calls routes the app does not have: {missing}"


def test_ui_covers_the_feature_endpoints():
    calls = {path for _, path in api_calls()}
    for path in (
        "/api/recommendations",
        "/api/recommendations/history",
        "/api/tickets/generate",
        "/api/live",
        "/api/simulate",
        "/api/simulate/datasets",
        "/api/simulate/recent/prepare",
        "/api/simulate/recent/status",
        "/api/wallet",
        "/api/wallet/bet",
        "/api/wallet/deposit",
        "/api/wallet/reset",
        "/api/predictions",
        "/api/results",
        "/api/benchmark",
    ):
        assert path in calls, path
    assert any(path.startswith("/api/live/") for path in calls)
    assert any(path.startswith("/api/analysis/") for path in calls)
    assert any(path.startswith("/api/analyze/") for path in calls)


def test_static_pages_are_served_with_the_csp(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(create_app(Settings(api_key="", database=tmp_path / "web.db"))) as client:
        for path in ["/", "/app.css", *[f"/{p.name}" for p in SCRIPTS]]:
            response = client.get(path)
            assert response.status_code == 200, path
            csp = response.headers["content-security-policy"]
            assert "script-src 'self'" in csp and "style-src 'self'" in csp


# --- routes and structure -------------------------------------------------------------------


def test_every_hash_route_has_a_page_and_a_nav_link():
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    everything = "\n".join(sources().values())
    for route in ("meciuri", "live", "bilete", "simulator", "portofel", "rezultate", "metoda"):
        assert f"'/{route}'" in app_js, route
        assert f'href="#/{route}"' in html, route
    for renderer in re.findall(r"=> (render\w+)\(", app_js):
        assert re.search(rf"^(async )?function {renderer}\(", everything, re.M), renderer
    assert "/^\\/meci\\/(.+)$/" in app_js


def test_timers_are_cleared_when_leaving_a_page():
    for name, source in sources().items():
        if "setInterval(" in source:
            assert "clearInterval(" in source and "onLeave(" in source, name


def test_user_text_is_romanian_with_disclaimer_on_betting_pages():
    everything = "\n".join(sources().values())
    for text in (
        "nu garanții",
        "18+",
        "anulat",
        "Joacă în portofelul virtual",
        "Regenerează",
        "Altă variantă",
        "cotă minimă",
        "A ținut",
    ):
        assert text in everything, text
    # Every betting page renders the disclaimer.
    for name in ("home.js", "live.js", "tickets.js", "simulator.js", "wallet.js", "board.js"):
        assert "disclaimerBox(" in (WEB / name).read_text(encoding="utf-8"), name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse(path):
    check = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
