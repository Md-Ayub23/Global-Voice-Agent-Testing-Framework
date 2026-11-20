"""Guards for every web address a user types into Vocto (the agent's URL, tool upstreams, the backend's
REST base, the log URL, alert webhooks) before Vocto connects to it: server-side request forgery (SSRF).

Without them, anyone who can edit an agent's settings could point Vocto at addresses only the server can
reach and read the answers back (a test connection shows what came back): the cloud's metadata service
(instance credentials), the server's own admin ports, or files (file:// URLs).

Rules (urlopen() and check_url() apply them; redirects are checked again at every hop):
  - only http/https (and ws/wss for voice connections); never file:, ftp:, data: or anything else;
  - never link-local or cloud metadata addresses (169.254.0.0/16, fe80::/10, 100.100.100.200,
    fd00:ec2::254, metadata.google.internal), multicast, or "this network" (0.0.0.0/8);
  - VOCTO_URL_POLICY=server (the default under `vatf serve`) also refuses loopback (the Vocto server
    itself), unless the host is listed in VOCTO_URL_ALLOW. The local dashboard (`vatf ui`, policy
    "local") allows loopback and private addresses, because the agent under test usually runs right
    there on the same computer.
  - VOCTO_URL_ALLOW: comma-separated hosts or networks (CIDR) always allowed (e.g. an agent on
    10.0.4.12, or "localhost" on a server); VOCTO_URL_DENY: more hosts or networks to refuse.
Every name is resolved and every address it resolves to is checked.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

HTTP_SCHEMES = ("http", "https")
WS_SCHEMES = ("ws", "wss")
METADATA_HOSTS = {"metadata.google.internal", "metadata", "metadata.azure.com", "instance-data",
                  "instance-data.ec2.internal"}
ALWAYS_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "169.254.0.0/16", "100.100.100.200/32", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "fe80::/10", "fd00:ec2::254/128", "ff00::/8", "::/128")]
LOOPBACK = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128")]
MAX_RESPONSE = 20 * 1024 * 1024


class BlockedURL(urllib.error.URLError):
    """The address is not allowed (see the module notes). A URLError, so callers' error handling applies."""

    def __init__(self, reason: str):
        super().__init__(reason)


def policy() -> str:
    return (os.environ.get("VOCTO_URL_POLICY") or "local").strip().lower()


def _nets(raw: str | None) -> tuple[set[str], list]:
    hosts, nets = set(), []
    for item in (raw or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            hosts.add(item)
    return hosts, nets


def _in(ip, nets: Iterable) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def _resolve(host: str) -> list:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURL(f"can't find the address {host!r}: {e}") from None
    out = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        out.append(ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip)
    return out


def check_url(url: str, schemes: tuple[str, ...] = HTTP_SCHEMES) -> str:
    """Returns the URL when it may be used, else raises BlockedURL saying why."""
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme.lower() not in schemes:
        raise BlockedURL(f"only {' or '.join(s + '://' for s in schemes)} addresses are allowed, not {parts.scheme or 'none'}:")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise BlockedURL(f"{url!r} has no host name")
    allow_hosts, allow_nets = _nets(os.environ.get("VOCTO_URL_ALLOW"))
    deny_hosts, deny_nets = _nets(os.environ.get("VOCTO_URL_DENY"))
    if host in deny_hosts:
        raise BlockedURL(f"{host} is on the list of addresses Vocto may not call (VOCTO_URL_DENY)")
    if host in METADATA_HOSTS:
        raise BlockedURL(f"{host} is a cloud metadata address; Vocto never calls it")
    if host in allow_hosts:
        return url
    server = policy() == "server"
    for ip in _resolve(host):
        if _in(ip, deny_nets):
            raise BlockedURL(f"{host} ({ip}) is on the list of addresses Vocto may not call (VOCTO_URL_DENY)")
        if _in(ip, allow_nets):
            continue
        if _in(ip, ALWAYS_BLOCKED):
            raise BlockedURL(f"{host} ({ip}) is a link-local, metadata or reserved address; Vocto never calls it")
        if server and (_in(ip, LOOPBACK) or ip.is_unspecified):
            raise BlockedURL(f"{host} ({ip}) is this server itself; add it to VOCTO_URL_ALLOW if the agent "
                             "really runs here")
    return url


_PLAIN_HEADERS = {"accept", "accept-language", "user-agent", "content-type"}


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(urllib.parse.urljoin(req.full_url, newurl))
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            # Never carry the caller's own headers to another host: any of them may hold a secret
            # (a profile's headers can have any name, like X-Clinic-Token: ${TOKEN}). Only plain ones stay.
            if urllib.parse.urlsplit(new.full_url).netloc != urllib.parse.urlsplit(req.full_url).netloc:
                for store in (new.headers, new.unredirected_hdrs):
                    for name in list(store):
                        if name.lower() not in _PLAIN_HEADERS:
                            store.pop(name, None)
        return new


def _opener() -> urllib.request.OpenerDirector:
    # Only http and https handlers: no file://, ftp:// or data: by construction.
    handlers = [urllib.request.ProxyHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                urllib.request.HTTPDefaultErrorHandler(), _GuardedRedirect(), urllib.request.HTTPErrorProcessor()]
    opener = urllib.request.OpenerDirector()
    for h in handlers:
        opener.add_handler(h)
    return opener


def urlopen(req: urllib.request.Request | str, timeout: float = 20):
    """urllib.request.urlopen with the rules above (and their check at every redirect)."""
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    check_url(url)
    return _opener().open(req, timeout=timeout)


def read_limited(resp, limit: int = MAX_RESPONSE) -> bytes:
    """The answer's body, refusing one bigger than `limit` (a server can't fill Vocto's memory)."""
    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise BlockedURL(f"the answer is larger than {limit // (1024 * 1024)} MB")
    return raw
