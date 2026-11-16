"""What a plugin source's transport refusals say, and that each says what happened.

``fetch_plugin_source`` runs the real egress guard and the real
``PinnedSessionTransport``; only the socket underneath is replaced, so nothing
leaves the machine. That transport raises ``OAuthHopBlocked`` for three unlike
reasons (a request for another host, a compressed answer, an oversized answer),
and the exception carries a ``HopRefusal`` for each. The sentence the route
returns names the rule that was broken and none of the caller's URL, so each
must arrive as its own sentence: one fixed sentence for the whole type claimed
something about an answer that, for a host refusal, never arrived.
"""

from __future__ import annotations

import socket

import httpx2
import pytest

from src.server.services.mcp_oauth.http import OAuthHopBlocked
from src.server.services.plugins import fetch
from src.server.services.plugins.errors import PluginFatal

SECRET = "Zq8mW3xLp0vTn5Rk"
PIECES = {SECRET.lower()[i : i + 8] for i in range(len(SECRET) - 7)}


def _assert_no_piece(text: str) -> None:
    """No run of 8 characters of the sentinel, in either case."""
    assert not [p for p in PIECES if p in text.lower()], text


class _Body(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _Socket:
    """Stands in for the wire: records every request that reaches it."""

    def __init__(self, monkeypatch, *, headers=None, chunks=None) -> None:
        self.requests: list[httpx2.Request] = []
        answer_headers = headers or {}

        def getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        async def _handle(transport_self, request):
            self.requests.append(request)
            return httpx2.Response(
                200,
                headers=answer_headers,
                stream=_Body(chunks if chunks is not None else [b"zip"]),
                request=request,
            )

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _handle)


async def _refusal(url: str) -> str:
    with pytest.raises(PluginFatal) as caught:
        await fetch.fetch_plugin_source(url)
    return str(caught.value)


@pytest.mark.asyncio
async def test_a_host_the_transport_refuses_is_not_described_as_an_answer(monkeypatch):
    """``xn--bcher-kva.example`` is pinned under its punycode name while the
    request's URL reports it in Unicode, so the transport refuses the very host
    the guard approved, and refuses it before sending. The sentence says that,
    and says nothing of a compressed or oversized answer that never came."""
    wire = _Socket(monkeypatch)

    sentence = await _refusal("https://xn--bcher-kva.example/pkg.zip")

    assert wire.requests == []  # the refusal is on this side of the wire
    assert "compressed" not in sentence
    assert "larger than" not in sentence
    assert "host name" in sentence
    assert "nothing was sent" in sentence
    assert "xn--bcher" not in sentence  # and it is still the caller's text it leaves out


@pytest.mark.asyncio
async def test_a_compressed_answer_is_said_to_be_compressed(monkeypatch):
    _Socket(monkeypatch, headers={"content-encoding": "gzip"})

    sentence = await _refusal(f"https://{SECRET}.example/pkg.zip")

    assert "compressed" in sentence
    assert "larger than" not in sentence
    _assert_no_piece(sentence)


@pytest.mark.asyncio
async def test_an_oversized_answer_is_said_to_be_oversized_at_the_cap_in_force(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_PACKAGE_BYTES", 1024 * 1024)
    _Socket(monkeypatch, chunks=[b"x" * 1024 * 1024, b"x"])

    sentence = await _refusal(f"https://{SECRET}.example/pkg.zip")

    assert "larger than 1 MiB" in sentence
    assert "compressed" not in sentence
    _assert_no_piece(sentence)


@pytest.mark.asyncio
async def test_a_refusal_with_no_code_makes_no_claim_about_the_answer(monkeypatch):
    """A raise site that does not say which rule it enforces still gets a true
    sentence: that the transport refused, and nothing more."""
    _Socket(monkeypatch)

    async def _refuses(self, url, **kwargs):
        raise OAuthHopBlocked(f"{SECRET} did something new")

    monkeypatch.setattr(httpx2.AsyncClient, "get", _refuses)

    sentence = await _refusal("https://vendor.test/pkg.zip")

    assert "compressed" not in sentence
    assert "larger than" not in sentence
    _assert_no_piece(sentence)
    assert "refused" in sentence


def test_every_refusal_code_has_a_sentence_of_its_own():
    """A code added to the transport without a sentence here would fall to the
    generic one and read as a decision nobody made."""
    from src.server.services.mcp_oauth.http import HopRefusal

    generic = fetch.hop_refusal_sentence(HopRefusal.UNSPECIFIED)
    specific = {
        r: fetch.hop_refusal_sentence(r) for r in HopRefusal if r is not HopRefusal.UNSPECIFIED
    }

    assert generic
    assert set(HopRefusal) - {HopRefusal.UNSPECIFIED} == set(specific)
    assert generic not in specific.values()
    assert len(set(specific.values())) == len(specific)
    for sentence in [generic, *specific.values()]:
        _assert_no_piece(sentence)
