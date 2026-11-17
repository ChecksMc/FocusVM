"""Retrieve approved public HTTPS references for subsequent offline reading."""

import json
import os
import re
import socket
import ssl
from collections.abc import Iterable
from html.parser import HTMLParser
from http.client import HTTPException, HTTPSConnection
from ipaddress import ip_address
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast
from urllib.parse import urlsplit

from .local_state import atomic_write, file_lock, open_private_file

_MAX_BYTES = 2 * 1024 * 1024
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class ResearchInputError(ValueError):
    """A research destination or output is invalid."""


class ResearchError(RuntimeError):
    """A research reference or allowlist could not be read."""


class Research:
    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / "research-hosts.json"

    def hosts(self) -> tuple[str, ...]:
        try:
            with open(self.path, encoding="utf-8", opener=open_private_file) as stream:
                data: object = json.load(stream)
            if not isinstance(data, list):
                raise ValueError("Expected a list of approved hosts")
            return tuple(sorted({_domain(host) for host in cast("list[object]", data)}))
        except FileNotFoundError:
            return ()
        except (ValueError, RecursionError) as error:
            raise ResearchError(
                f"Could not read the research allowlist: {error}"
            ) from error

    def update_host(self, host: str, *, allow: bool) -> tuple[str, ...]:
        name = _domain(host)
        with file_lock(self.path):
            approved = set(self.hosts())
            if allow:
                approved.add(name)
            else:
                approved.discard(name)
            result = tuple(sorted(approved))
            atomic_write(self.path, json.dumps(result) + "\n")
            return result

    def save(self, url: str, output: Path) -> Path:
        host, target = _address(url)
        if output.suffix.lower() != ".md":
            raise ResearchInputError("Choose an output filename ending in .md")
        output = output.absolute()
        if os.path.lexists(output):
            raise ResearchInputError(f"Output already exists: {output}")

        with file_lock(self.path):
            if host not in self.hosts():
                raise ResearchInputError(f"Approve {host} before saving its references")
            content = _download(host, target)
        # Indented Markdown keeps the downloaded text inert when rendered as a document.
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent
        ) as stream:
            stream.write(f"Source: `{url.replace('`', '%60')}`\n\n")
            for line in content.splitlines():
                printable = "".join(
                    char for char in line.expandtabs() if char.isprintable()
                )
                stream.write(f"    {printable}\n")
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(stream.name, output)
            except FileExistsError as error:
                raise ResearchInputError(f"Output already exists: {output}") from error
        return output


def _domain(value: object) -> str:
    if not isinstance(value, str):
        raise ResearchInputError("Enter an exact public DNS hostname")
    host = value.lower()
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or labels[-1].isdigit()
        or not all(_DOMAIN.fullmatch(label) for label in labels)
    ):
        raise ResearchInputError(
            "Enter an exact public DNS hostname, such as en.wikipedia.org"
        )
    return host


def _address(url: str) -> tuple[str, str]:
    if (
        not url.startswith("https://")
        or not url.isascii()
        or len(url) > 4096
        or any(character.isspace() or not character.isprintable() for character in url)
        or "\\" in url
        or "#" in url
    ):
        raise ResearchInputError(
            "Use a public HTTPS URL with no fragment or credentials"
        )
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise ResearchInputError("Use a valid public HTTPS URL") from error
    if parts.hostname is None or parts.netloc.lower() != parts.hostname:
        raise ResearchInputError("Use a public HTTPS URL with no port or credentials")
    host = _domain(parts.hostname)
    target = parts.path or "/"
    if parts.query:
        target += f"?{parts.query}"
    return host, target


class _PublicHTTPS(HTTPSConnection):
    def connect(self) -> None:
        with_public_ip = False
        context = ssl.create_default_context()
        for family, kind, protocol, _, address in socket.getaddrinfo(
            self.host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
        ):
            ip = ip_address(address[0])
            if ip.version != 4 or not ip.is_global or ip.is_multicast or ip.is_reserved:
                continue
            with_public_ip = True
            stream = socket.socket(family, kind, protocol)
            try:
                stream.settimeout(6)
                stream.connect(address)
                self.sock = context.wrap_socket(stream, server_hostname=self.host)
                return
            except ssl.SSLCertVerificationError:
                stream.close()
                raise
            except OSError:
                stream.close()
        if with_public_ip:
            raise OSError("No public address accepted the connection")
        raise ResearchError(
            "The approved host did not resolve to a public IPv4 address"
        )


def _download(host: str, target: str) -> str:
    connection = _PublicHTTPS(host, timeout=6)
    try:
        connection.request("GET", target, headers={"User-Agent": "FocusVM"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise ResearchError(
                    f"Reference server returned HTTP {response.status}. Use the final approved HTTPS URL."
                )
            kind = response.headers.get_content_type()
            if kind not in ("text/plain", "text/markdown", "text/html"):
                raise ResearchError("Choose an HTML, Markdown, or plain-text reference")
            body = response.read(_MAX_BYTES + 1)
            if len(body) > _MAX_BYTES:
                raise ResearchError("Reference exceeds the 2 MiB download limit")
            encoding = response.headers.get_content_charset() or "utf-8"
            text = body.decode(encoding, errors="replace")
    except (OSError, HTTPException, LookupError, ValueError) as error:
        raise ResearchError(
            f"Could not retrieve the reference from {host}: {error}"
        ) from error
    finally:
        connection.close()
    if kind == "text/html":
        parser = _Text()
        parser.feed(text)
        parser.close()
        text = _collapse_blank_lines(
            " ".join(line.split()) for line in "".join(parser.parts).splitlines()
        )
    return text


def _collapse_blank_lines(lines: Iterable[str]) -> str:
    """Keep paragraph breaks while dropping layout-only runs of empty lines."""
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


class _Text(HTMLParser):
    _hidden = {"script", "style", "template", "svg", "math", "iframe", "object"}
    # Most pages lay out text with generic containers. Without line breaks for
    # these elements, a typical page collapses into one unreadable line.
    _blocks = {
        "address", "article", "aside", "blockquote", "br", "caption", "dd",
        "details", "div", "dl", "dt", "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
        "ol", "p", "pre", "section", "summary", "table", "td", "th", "title",
        "tr", "ul",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._hidden:
            self.hidden += 1
        elif tag in self._blocks and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._hidden:
            self.hidden = max(0, self.hidden - 1)
        elif tag in self._blocks and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)
