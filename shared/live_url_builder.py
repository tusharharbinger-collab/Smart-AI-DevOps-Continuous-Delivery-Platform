"""
shared/live_url_builder.py

Real gap found live (2026-09-17): every call site building a project's
live_url (api-gateway's `_live_url()` for the displayed link, plus
pipeline-worker's and policy-controller's own copies for the actual
`verify_live_url` check) concatenated `base_url + path_prefix` with no
trailing slash. A project's own app can serve its page fine at the bare
prefix (this platform's own onboarding runbook already tells every project
to use a catch-all matched on the last path segment, precisely so it does),
but a real browser only resolves the page's own RELATIVE asset references
(`<link href="style.css">`, `<script src="app.js">`) correctly when the
current URL ends in "/" — without it, the browser resolves them against the
PARENT path instead, and they 404 even though the page itself loaded.
Confirmed live: a real onboarded app rendered completely unstyled with no
working JS at the bare (no-trailing-slash) live_url the platform itself
handed the user.

Deliberately its own module, separate from shared/live_url_check.py: this
is pure string logic with zero external dependencies, but live_url_check.py
imports `requests` (needed for its own verify_live_url) — api-gateway,
which only ever needs the URL-building half for the displayed link, has no
`requests` dependency in its own requirements.txt, so importing
build_live_url from live_url_check.py transitively crashed its container
at startup (ModuleNotFoundError: No module named 'requests', caught live).
"""


def build_live_url(base_url: str | None, path_prefix: str | None) -> str | None:
    if not base_url or not path_prefix:
        return None
    normalized_prefix = path_prefix if path_prefix.endswith("/") else f"{path_prefix}/"
    return f"http://{base_url}{normalized_prefix}"
