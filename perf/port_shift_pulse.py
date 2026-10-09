"""Port the standalone Shift-Pulse.html into LDIS as a served page.

The dashboard is a UI shell that fetches nothing itself: the Tampermonkey
data provider writes into ~287 element ids and calls getWarehouseId() /
getBucketConfig() on the page. Those ids and functions are the contract
with the provider, so markup and script are carried over as they are.
Only the packaging changes:

  * every CSS rule is scoped under .sp-root, because a stylesheet written
    for a standalone document styles `*` and `body` and would otherwise
    restyle the whole console around it;
  * the markup becomes a Jinja partial rendered inside the LDIS shell;
  * shared settings move from localStorage to the server (the shim lives
    in the page template, not here).

Re-run this against a newer Shift-Pulse.html rather than hand-editing the
generated files:

    python perf/port_shift_pulse.py <Shift-Pulse.html> static templates
"""
import io, os, re, sys

src_path, out_static, out_templates = sys.argv[1], sys.argv[2], sys.argv[3]
doc = io.open(src_path, encoding="utf-8").read()

STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)

styles = STYLE_RE.findall(doc)
scripts = SCRIPT_RE.findall(doc)

stripped = SCRIPT_RE.sub("", STYLE_RE.sub("", doc))
body = re.search(r"<body[^>]*>(.*?)</body>", stripped, re.DOTALL | re.IGNORECASE).group(1)


# The standalone tool predates the console's dark theme: it paints white
# and light-grey surfaces with dark navy text. Overriding that by hand
# loses a specificity fight (its own rules reach `thead tr:first-child
# th`), so the dark layer is generated from the stylesheet itself —
# every rule that sets a light surface or dark-on-light text gets a
# counterpart with the same selector under html[data-theme="dark"],
# which is always one step more specific than the original.
#
# Only surface, text and border colours are remapped. The dashboard's
# status colours — the reds, ambers and greens that tell a manager a
# process is behind — are deliberately left alone: those carry meaning
# the rollout deck trains people on.
LIGHT_SURFACES = {
    "#ffffff": "var(--surface-solid)", "#fff": "var(--surface-solid)",
    "white": "var(--surface-solid)",
    "#f5f5f5": "var(--surface)", "#f0f0f0": "var(--surface-raised)",
    "#f9f9f9": "rgba(255,255,255,.04)", "#fafafa": "rgba(255,255,255,.03)",
    "#f8f8f8": "rgba(255,255,255,.03)", "#eeeeee": "var(--surface-raised)",
    "#eee": "var(--surface-raised)", "#e0e0e0": "var(--surface-raised)",
}
DARK_INK = {
    "#232f3e", "#1b2838", "#16213e", "#1e3a5f", "#2a4060",
    "#333", "#333333", "#000", "#000000", "#222", "#111",
}
LIGHT_BORDERS = {"#e0e0e0", "#ddd", "#dddddd", "#ccc", "#cccccc", "#eee", "#eeeeee"}


def dark_layer(scoped_css):
    """Emit a dark-theme counterpart for every rule that paints a light
    surface, dark-on-light text, or a light border."""
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", scoped_css)
    out = []
    for selector, body in rules:
        selector = selector.strip()
        if not selector or selector.startswith("@") or "%" == selector.strip():
            continue
        changed = []
        for decl in body.split(";"):
            if ":" not in decl:
                continue
            prop, _, value = decl.partition(":")
            prop, value = prop.strip().lower(), value.strip()
            low = value.lower()
            if prop in ("background", "background-color"):
                for light, token in LIGHT_SURFACES.items():
                    if low == light or low.startswith(light + " "):
                        changed.append(f"{prop}: {token}")
                        break
            elif prop == "color" and low in DARK_INK:
                changed.append("color: #e9e9eb")
            elif prop in ("border-color", "border-bottom-color", "border-top-color"):
                if low in LIGHT_BORDERS:
                    changed.append(f"{prop}: var(--hairline)")
            elif prop.startswith("border") and any(b in low for b in LIGHT_BORDERS):
                for b in LIGHT_BORDERS:
                    if b in low:
                        changed.append(f"{prop}: {value.lower().replace(b, 'var(--hairline)')}")
                        break
        if changed:
            dark_selector = ",\n".join(
                'html[data-theme="dark"] ' + part.strip()
                for part in selector.split(",") if part.strip())
            out.append(dark_selector + " {\n  " + ";\n  ".join(changed) + ";\n}")
    return "\n".join(out)


def scope_css(css, root=".sp-root"):
    """Prefix every top-level selector so the stylesheet cannot reach
    outside the dashboard. At-rules (only @keyframes here) pass through
    whole — their 'selectors' are percentages, not elements."""
    out, i, n = [], 0, len(css)
    while i < n:
        at, brace = css.find("@", i), css.find("{", i)
        if brace == -1:
            out.append(css[i:])
            break
        if at != -1 and at < brace:
            depth, k = 0, css.index("{", at)
            while k < n:
                if css[k] == "{":
                    depth += 1
                elif css[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            out.append(css[i:k + 1])
            i = k + 1
            continue
        selector, end = css[i:brace], css.find("}", brace)
        parts = []
        for sel in selector.split(","):
            s = sel.strip()
            if not s:
                continue
            if s == "*":
                parts.append(root + " *")
            elif s in ("html", "body", ":root"):
                parts.append(root)
            elif s.startswith("body"):
                parts.append(root + s[4:])
            else:
                parts.append(root + " " + s)
        out.append("\n" + ",\n".join(parts) + " " + css[brace:end + 1])
        i = end + 1
    return "".join(out)


scoped = scope_css("\n".join(styles))
io.open(os.path.join(out_static, "shift_pulse.css"), "w", encoding="utf-8").write(
    "/* Shift Pulse dashboard styles, generated by perf/port_shift_pulse.py\n"
    "   from the standalone Shift-Pulse.html. Every rule is scoped under\n"
    "   .sp-root: the original styles `*` and `body` because it owned the\n"
    "   whole document. The dark-theme block at the end is generated from\n"
    "   these same rules. Regenerate rather than hand-editing. */\n"
    + scoped
    + "\n\n/* ---- dark theme, generated from the rules above ---- */\n"
    + dark_layer(scoped) + "\n")

# ---------------------------------------------------------------- patches
# Hosting changes one thing the standalone tool could assume: its saved
# configuration lived in one manager's browser, so a malformed value
# broke only that manager. Here the same keys are shared across the site
# from the database, and an uncaught TypeError while reading one would
# take Shift Pulse down for everyone on the FC. These two loaders are
# therefore made to validate what they read. Each patch asserts, so a
# regeneration against a changed upstream file fails loudly instead of
# silently dropping the hardening.
JS_PATCHES = [
    (
        """            if (saved) {
                try {
                    const parsed = JSON.parse(saved);
                    return parsed;
                } catch (e) {
                    console.warn('Failed to parse saved bucket config:', e);""",
        """            if (saved) {
                try {
                    const parsed = JSON.parse(saved);
                    /* The stored value is now shared across the site rather
                       than private to one browser, so a single bad write
                       would otherwise take Shift Pulse down for everybody on
                       the FC. Anything that is not a usable bucket list is
                       ignored in favour of the defaults. */
                    if (parsed && Array.isArray(parsed.buckets)) {
                        parsed.buckets = parsed.buckets.filter(function (b) {
                            return b && typeof b === 'object';
                        }).map(function (b) {
                            return {
                                id: b.id || ('bucket_' + Math.random().toString(36).slice(2, 8)),
                                name: b.name || 'Unnamed',
                                functions: Array.isArray(b.functions) ? b.functions.filter(function (f) {
                                    return f && f.name;
                                }) : []
                            };
                        });
                        return parsed;
                    }
                    console.warn('Saved bucket config has no buckets; using defaults:', parsed);
                } catch (e) {
                    console.warn('Failed to parse saved bucket config:', e);""",
    ),
    (
        """                // Position highlight
                highlight.style.display = 'block';
                highlight.style.left = (rect.left - pad) + 'px';
                highlight.style.top = (rect.top - pad) + 'px';
                highlight.style.width = (rect.width + pad * 2) + 'px';
                highlight.style.height = (rect.height + pad * 2) + 'px';""",
        """                // Position highlight. Clamped to the viewport: the
                // highlight is position:fixed, and standing alone the
                // dashboard filled the window so its target rect never
                // reached past the edge. Inside the console shell it can,
                // and a fixed box hanging over the right edge gives the
                // whole page a horizontal scrollbar — nav and all — for
                // the length of the tour.
                const hlLeft = Math.max(0, rect.left - pad);
                const hlTop = Math.max(0, rect.top - pad);
                highlight.style.display = 'block';
                highlight.style.left = hlLeft + 'px';
                highlight.style.top = hlTop + 'px';
                highlight.style.width = Math.max(0, Math.min(rect.width + pad * 2,
                    document.documentElement.clientWidth - hlLeft)) + 'px';
                highlight.style.height = Math.max(0, Math.min(rect.height + pad * 2,
                    document.documentElement.clientHeight - hlTop)) + 'px';""",
    ),
    (
        """            if (saved) {
                try {
                    return JSON.parse(saved);
                } catch (e) {
                    console.warn('Failed to parse saved shift settings:', e);""",
        """            if (saved) {
                try {
                    const parsed = JSON.parse(saved);
                    /* Same reasoning as loadBucketConfig: a shared value that
                       is missing a shift would break the page for the whole
                       site, so each shift falls back to its default
                       individually rather than all-or-nothing. */
                    if (parsed && typeof parsed === 'object') {
                        const merged = JSON.parse(JSON.stringify(DEFAULT_SHIFTS));
                        ['night', 'early', 'late'].forEach(function (shift) {
                            if (parsed[shift] && typeof parsed[shift] === 'object') {
                                merged[shift] = parsed[shift];
                            }
                        });
                        return merged;
                    }
                    console.warn('Saved shift settings are not usable; using defaults:', parsed);
                } catch (e) {
                    console.warn('Failed to parse saved shift settings:', e);""",
    ),
]

js_body = "\n".join(f"\n/* ---- block {i + 1} ---- */\n{s}" for i, s in enumerate(scripts))
for _before, _after in JS_PATCHES:
    assert js_body.count(_before) == 1, (
        "the upstream Shift-Pulse.html changed around a hosting patch; "
        "re-check it before regenerating:\n" + _before[:120]
    )
    js_body = js_body.replace(_before, _after)

io.open(os.path.join(out_static, "shift_pulse.js"), "w", encoding="utf-8").write(
    "// Shift Pulse dashboard script, generated by perf/port_shift_pulse.py\n"
    "// from the standalone Shift-Pulse.html. Carried over unchanged apart\n"
    "// from the JS_PATCHES in that script, which make the two loaders of\n"
    "// shared configuration validate what they read — the values are now\n"
    "// shared site-wide, so one bad write would otherwise break the\n"
    "// dashboard for everyone. The Tampermonkey data provider writes into\n"
    "// the element ids this file and the template define, and calls\n"
    "// getWarehouseId()/getBucketConfig() on the page — that is a contract\n"
    "// with the provider, so do not rename them. Regenerate rather than\n"
    "// hand-editing.\n"
    + js_body + "\n")

# A couple of inline style attributes in the markup carry the same solid
# light tints the stylesheet did, and inline styles are beyond the reach
# of the generated dark layer. Swapped for translucent equivalents, which
# sit correctly on either theme.
INLINE_TINTS = {
    "#fff3cd": "rgba(255,159,10,.14)",   # warning amber
    "#f8d7da": "rgba(255,69,58,.14)",    # error red
    "#d4edda": "rgba(48,209,88,.14)",    # success green
    "#d1ecf1": "rgba(41,151,255,.14)",   # info blue
}
for _light, _token in INLINE_TINTS.items():
    body = body.replace(_light, _token)

io.open(os.path.join(out_templates, "_shift_pulse_body.html"), "w", encoding="utf-8").write(
    "{# Shift Pulse markup, generated by perf/port_shift_pulse.py from the\n"
    "   standalone Shift-Pulse.html. The element ids are the contract with\n"
    "   the Tampermonkey data provider. Regenerate rather than hand-editing. #}\n"
    + body.strip() + "\n")

ids = set(re.findall(r'id="([^"]+)"', body))
print(f"styles : {len(styles)} block(s) -> {len(scope_css(chr(10).join(styles)).splitlines())} lines")
print(f"scripts: {len(scripts)} block(s)")
print(f"markup : {len(body.strip().splitlines())} lines, {len(ids)} unique element ids")
