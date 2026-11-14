from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from focusvm.research import Research, ResearchError, ResearchInputError


def test_research_exact_allowlist_and_publishes_inert_text(tmp_path: Path) -> None:
    research = Research(tmp_path)
    output = tmp_path / "reference.md"
    url = "https://docs.example.org/reference"
    with patch(
        "focusvm.research._download",
        return_value="<img src='https://elsewhere.example'>",
    ) as download:
        with pytest.raises(ResearchInputError, match="Approve"):
            research.save(url, output)
        assert research.update_host("EXAMPLE.ORG", allow=True) == ("example.org",)
        with pytest.raises(ResearchInputError, match="Approve"):
            research.save(url, output)
        download.assert_not_called()
        research.update_host("docs.example.org", allow=True)
        assert research.save(url, output) == output
        download.assert_called_once_with("docs.example.org", "/reference")
        assert output.read_text().splitlines()[2].startswith("    <img")
        with pytest.raises(ResearchInputError, match="already exists"):
            research.save(url, output)
        assert research.update_host("docs.example.org", allow=False) == ("example.org",)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",
        "https://u@example.org/",
        "https://example.org:443/",
        "https://127.0.0.1/",
        "https://[broken/",
    ],
)
def test_research_rejects_nonpublic_url_syntax(tmp_path: Path, url: str) -> None:
    with pytest.raises(ResearchInputError):
        Research(tmp_path).save(url, tmp_path / "reference.md")


def test_research_rejects_private_dns_answers_before_connecting(tmp_path: Path) -> None:
    research = Research(tmp_path)
    research.update_host("example.org", allow=True)
    with (
        patch(
            "focusvm.research.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.168.1.1", 443))],
        ),
        patch("focusvm.research.socket.socket") as connect,
        pytest.raises(ResearchError, match="public IPv4 address"),
    ):
        research.save("https://example.org", tmp_path / "reference.md")
    connect.assert_not_called()


def test_research_refuses_redirect_and_strips_active_html(tmp_path: Path) -> None:
    research = Research(tmp_path)
    research.update_host("example.org", allow=True)
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 302
    response.headers = Message()
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.read.return_value = (
        b"<h1>Reference</h1><script>bad()</script><p>Example</p>"
    )
    output = tmp_path / "reference.md"
    with patch("focusvm.research._PublicHTTPS") as connection:
        connection.return_value.getresponse.return_value = response
        with pytest.raises(ResearchError, match="HTTP 302"):
            research.save("https://example.org", output)
        assert not output.exists()
        response.status = 200
        research.save("https://example.org", output)
    assert "Reference" in output.read_text()
    assert "Example" in output.read_text()
    assert "bad()" not in output.read_text()
