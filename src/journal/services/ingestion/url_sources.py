"""URL-fetching ingest paths for ``IngestionService``.

Mixin holding the three URL entry points and the shared ``_download``
helper. The SSRF guard (``_validate_public_url``) is module-level so
it can be exercised directly by tests.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

if TYPE_CHECKING:
    from email.message import Message
    from http.client import HTTPResponse

    from journal.models import Entry

log = logging.getLogger(__name__)

#: Maximum number of redirect hops ``_download`` will follow.
_MAX_REDIRECTS = 5


class _RedirectDisabledHandler(HTTPRedirectHandler):
    """Redirect handler that refuses to follow redirects.

    ``redirect_request`` returning ``None`` means urllib does not
    handle the 3xx itself, so it surfaces as an ``HTTPError`` carrying
    the response headers. ``_download`` catches it and re-issues the
    request manually, re-running the SSRF guard on every hop —
    urllib's built-in redirect following would silently skip that
    validation.
    """

    def redirect_request(  # type: ignore[override]  # noqa: PLR0913
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RedirectDisabledHandler())


def urlopen(req: Request) -> HTTPResponse:
    """Open ``req`` WITHOUT following redirects (3xx raises HTTPError).

    Module-level (and deliberately named like ``urllib.request.urlopen``,
    which it replaces here) so tests can patch the network boundary in
    one place.
    """
    return _NO_REDIRECT_OPENER.open(req)  # noqa: S310


def _validate_public_url(url: str) -> None:
    """Reject URLs that would expose the server to SSRF.

    Resolves the hostname via DNS and refuses to continue if any of
    its addresses are loopback (127.0.0.0/8, ::1), private (RFC1918 +
    RFC 4193), link-local (169.254.0.0/16 — includes cloud metadata
    endpoints), multicast, reserved, or unspecified. Non-HTTP(S)
    schemes are also rejected, so ``file://``, ``gopher://``, and
    friends are blocked wholesale.

    This is called from ``_download()`` before any network traffic —
    once for the original URL and again for every redirect target
    (``_download`` disables urllib's automatic redirect following and
    re-validates each hop) — so an attacker cannot use a
    journal-server ingest endpoint, directly or via a redirect chain,
    to pivot into internal services on the host VM or the cloud
    metadata IP. It does NOT defend against DNS rebinding between
    resolution and connection — an attacker with control of DNS
    could return a public IP to this check and a private IP to
    urlopen — but closing that is a socket-level fix that requires
    patching urllib's connection pathway, which is out of scope for
    a personal tool. Loopback and RFC1918 are the realistic threat
    surface, and they are closed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme must be http or https, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {url!r}")

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError as e:
        raise ValueError(
            f"Failed to resolve {parsed.hostname!r}: {e}"
        ) from e

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # getaddrinfo returned something that isn't an IP — skip
            # it. The socket layer will refuse to connect anyway.
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Refusing to fetch {url!r} — host {parsed.hostname} "
                f"resolved to non-public address {ip_str}"
            )


class _UrlIngestMixin:
    """URL-source ingest paths: image / voice / multi-page-from-URLs.

    Each entry point defers to the corresponding bytes-based
    ``ingest_*`` method after fetching the URL through the shared
    ``_download`` helper. ``https://files.slack.com`` URLs (exact host)
    get a Bearer-token header when ``self._slack_bot_token`` is set;
    the SSRF guard applies to every fetch and every redirect hop.
    """

    def ingest_image_from_url(
        self,
        url: str,
        date: str,
        media_type: str | None = None,
        *,
        user_id: int = 1,
    ) -> Entry:
        """Download an image from a URL and ingest it."""
        data, resolved_type = self._download(url, media_type)
        return self.ingest_image(  # type: ignore[attr-defined]
            data, resolved_type, date, user_id=user_id,
        )

    def ingest_multi_page_entry_from_urls(
        self,
        urls: list[str],
        date: str,
        media_types: list[str | None] | None = None,
        *,
        user_id: int = 1,
    ) -> Entry:
        """Download a list of page images from URLs and ingest them as one entry.

        Each URL is downloaded (with Slack bearer auth where
        applicable), then the raw bytes are handed to
        ``ingest_multi_page_entry`` which OCRs each page individually
        and combines them into a single entry with one page record
        per image.

        Args:
            urls: Ordered list of image URLs, one per page.
            date: Journal entry date (ISO 8601).
            media_types: Optional per-URL MIME type overrides. If
                provided, must have the same length as ``urls``;
                ``None`` entries fall back to the Content-Type
                returned by the server.
        """
        if not urls:
            raise ValueError("At least one URL is required")
        if media_types is not None and len(media_types) != len(urls):
            raise ValueError(
                "media_types must have the same length as urls when provided"
            )

        log.info(
            "Downloading %d pages for multi-page entry (date=%s)",
            len(urls), date,
        )
        images: list[tuple[bytes, str]] = []
        for i, url in enumerate(urls):
            override = media_types[i] if media_types is not None else None
            data, resolved_type = self._download(url, override)
            images.append((data, resolved_type))

        return self.ingest_multi_page_entry(  # type: ignore[attr-defined]
            images, date, user_id=user_id,
        )

    def ingest_voice_from_url(
        self,
        url: str,
        date: str,
        media_type: str | None = None,
        language: str = "en",
        *,
        user_id: int = 1,
    ) -> Entry:
        """Download audio from a URL and ingest it."""
        data, resolved_type = self._download(url, media_type)
        return self.ingest_voice(  # type: ignore[attr-defined]
            data, resolved_type, date, language, user_id=user_id,
        )

    def _download(
        self, url: str, media_type: str | None = None,
    ) -> tuple[bytes, str]:
        """Download a file from a URL, return (data, media_type).

        SSRF protection: every URL — the original AND each redirect
        target — is validated against ``_validate_public_url`` before
        any socket is opened, so loopback/private/link-local targets
        (including cloud metadata endpoints like 169.254.169.254) are
        refused regardless of the caller. Redirects are followed
        manually (urllib's automatic following is disabled) and capped
        at ``_MAX_REDIRECTS`` hops.

        The Slack bearer token is attached per hop, and only when the
        hop's host is exactly ``files.slack.com`` over https — it is
        never leaked to other hosts that merely mention the Slack
        domain in their path/query, nor forwarded across a redirect
        off Slack.
        """
        log.info("Downloading from %s", url)
        current = url
        redirects = 0
        while True:
            _validate_public_url(current)
            try:
                req = Request(
                    current, headers={"User-Agent": "journal-server/0.1"},
                )
                parsed = urlparse(current)
                if (
                    parsed.scheme == "https"
                    and parsed.hostname == "files.slack.com"
                    and self._slack_bot_token  # type: ignore[attr-defined]
                ):
                    req.add_header(
                        "Authorization",
                        f"Bearer {self._slack_bot_token}",  # type: ignore[attr-defined]
                    )
                with urlopen(req) as resp:
                    data = resp.read()
                    if media_type is None:
                        media_type = resp.headers.get(
                            "Content-Type", "application/octet-stream",
                        )
            except HTTPError as e:
                location = e.headers.get("Location") if e.headers else None
                if 300 <= e.code < 400 and location:
                    redirects += 1
                    if redirects > _MAX_REDIRECTS:
                        raise ValueError(
                            f"Failed to download {url}: too many "
                            f"redirects (more than {_MAX_REDIRECTS})"
                        ) from e
                    current = urljoin(current, location)
                    log.info(
                        "Following redirect %d/%d to %s",
                        redirects, _MAX_REDIRECTS, current,
                    )
                    continue
                raise ValueError(
                    f"Failed to download {current}: HTTP {e.code}"
                ) from e
            except URLError as e:
                raise ValueError(
                    f"Failed to download {current}: {e.reason}"
                ) from e

            log.info(
                "Downloaded %d bytes (type: %s)", len(data), media_type,
            )
            return data, media_type
