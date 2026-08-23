"""The two HTML pages this API serves, and the CSP that lets them render.

Phase 7 set ``default-src 'none'`` on every response, which is the right policy for
a JSON API and the wrong one for Swagger UI: that page is a CDN script bundle, a CDN
stylesheet, a favicon and one inline ``<script>``, and ``'none'`` blocks all of
them. /docs and /redoc came out blank, with six blocked resources in the console,
and blocked resources on a documentation page look exactly like an application that
is down.

The relaxation is scoped to the documentation routes -- ``csp_for_path`` in
``app/middleware.py`` keeps every other route strict. These handlers replace
FastAPI's built-in ones for a single reason: **the policy is computed from the bytes
being served**. The inline script is allowed by the sha256 of its own contents, so
the page keeps working if FastAPI changes what it emits, and an inline script that
FastAPI did not write is still refused. A hardcoded hash would be a silent breakage
one dependency bump away, and ``'unsafe-inline'`` would give up the property that
makes the header worth setting.
"""

import base64
import hashlib
import re

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse

from app.middleware import CSP_HEADER, docs_csp

#: ``<script>`` blocks with no ``src`` -- i.e. the ones whose contents are in the
#: document and therefore need a hash. The negative lookahead is what excludes the
#: CDN tags, which are allowed by origin instead.
_INLINE_SCRIPT = re.compile(
    rb"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)


def inline_script_hashes(html: bytes) -> list[str]:
    """CSP source expressions for every inline script in ``html``.

    The hash is taken over the exact bytes between the tags, which is what a browser
    hashes -- not the trimmed text, not the tag. One character of difference and the
    script is refused, which is the intended behaviour and also why this is computed
    per response rather than written down.
    """
    return [
        f"'sha256-{base64.b64encode(hashlib.sha256(script).digest()).decode()}'"
        for script in (match.group(1) for match in _INLINE_SCRIPT.finditer(html))
    ]


def _page(html: HTMLResponse) -> HTMLResponse:
    """Attach the policy this exact page needs, and hand it back.

    The middleware only fills in a CSP when the response does not already carry one,
    so setting it here wins -- see ``LedgerlineMiddleware.__call__``.
    """
    html.headers[CSP_HEADER] = docs_csp(inline_script_hashes(html.body))
    return html


def register_docs_routes(app: FastAPI, *, openapi_url: str = "/openapi.json") -> None:
    """Serve /docs and /redoc ourselves so each page can carry its own CSP.

    Registered instead of FastAPI's built-ins, which are turned off in
    ``app/main.py`` with ``docs_url=None, redoc_url=None``. /openapi.json keeps its
    built-in route: it is JSON with nothing to hash, and the middleware gives it the
    relaxed policy by path.
    """

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return _page(
            get_swagger_ui_html(openapi_url=openapi_url, title=f"{app.title} - Swagger UI")
        )

    @app.get("/redoc", include_in_schema=False)
    async def redoc() -> HTMLResponse:
        return _page(get_redoc_html(openapi_url=openapi_url, title=f"{app.title} - ReDoc"))
