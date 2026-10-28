"""Where Chromium is, today.

The platform image moves it — it was chromium-1194/chrome-linux/chrome
and is now chromium-1243/chrome-linux64/chrome — so every test that
hardcoded a path broke at once when the container was rebuilt. Asking
the filesystem costs nothing and survives the next move.
"""
import os
import glob

_ENV = "LDIS_CHROMIUM"


def chromium_path():
    override = os.environ.get(_ENV)
    if override:
        return override
    roots = sorted(glob.glob("/opt/pw-browsers/chromium*"), reverse=True)
    for root in roots:
        for candidate in ("chrome-linux64/chrome", "chrome-linux/chrome",
                          "chrome-linux64/headless_shell", "chrome-linux/headless_shell"):
            path = os.path.join(root, candidate)
            if os.path.exists(path):
                return path
    found = glob.glob("/opt/pw-browsers/**/chrome", recursive=True)
    if found:
        return found[0]
    raise RuntimeError(
        f"no Chromium found under /opt/pw-browsers; set {_ENV} to its path")
