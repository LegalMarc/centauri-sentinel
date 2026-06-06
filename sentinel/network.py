import contextlib
import ipaddress
import re
import socket


def validate_printer_ip(v: str) -> str:
    """
    Validates a printer IP or hostname syntax only, preserving and returning
    the original string as-is.
    """
    v_str = v.strip()
    if not v_str:
        raise ValueError("printer_ip cannot be empty")

    ip = None
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(v_str)
    if ip is None:
        if re.match(r"^[\d\.]+$", v_str):
            raise ValueError(f"printer_ip must be a valid IP address or hostname: {v_str}")

        hostname_regex = re.compile(
            r"^(?:[a-zA-Z0-9]"
            r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
            r"[a-zA-Z0-9]"
            r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
        )
        if not hostname_regex.match(v_str):
            raise ValueError(f"printer_ip must be a valid IP address or hostname: {v_str}")

    return v_str


def resolve_and_validate_printer_ip(v: str) -> str:
    """
    Resolves a printer hostname or IP to a literal IP address, then validates
    it against SSRF risks.
    """
    v_str = v.strip()
    if not v_str:
        raise ValueError("printer_ip cannot be empty")

    ip = None
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(v_str)
    if ip is None:
        if v_str.lower() in ("localhost", "localhost.localdomain"):
            raise ValueError(f"SSRF Protection: Loopback hostnames are not allowed: {v_str}")

        try:
            resolved_ip_str = socket.gethostbyname(v_str)
            ip = ipaddress.ip_address(resolved_ip_str)
        except OSError as e:
            raise ValueError(f"SSRF Protection: Cannot resolve hostname {v_str}") from e

    # Unmap IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip.is_unspecified or str(ip) == "0.0.0.0" or str(ip) == "::":
        raise ValueError(f"SSRF Protection: Unspecified IP addresses are not allowed: {v_str}")

    if ip.is_link_local or str(ip) == "169.254.169.254" or ip.is_multicast or ip.is_loopback:
        raise ValueError(f"SSRF Protection: Disallowed IP address type: {v_str}")
    if ip.is_global:
        raise ValueError(f"SSRF Protection: Globally routable IPs are not allowed: {v_str}")

    return str(ip)


def validate_https(url: str) -> str:
    """Enforce HTTPS for external webhooks/URLs to prevent credential exposure."""
    import urllib.parse

    url = url.strip()
    if url:
        parsed = urllib.parse.urlparse(url)
        # Allow localhost, 127.0.0.1, internal docker hostnames (no dots), and private IPs over HTTP
        if parsed.hostname and parsed.scheme == "http":
            if (
                parsed.hostname in ("localhost", "127.0.0.1")
                or "." not in parsed.hostname
                or parsed.hostname.endswith((".local", ".lan", ".home", ".internal"))
            ):
                return url
            with contextlib.suppress(ValueError):
                ip = ipaddress.ip_address(parsed.hostname)
                if ip.is_private:
                    return url
        if not url.startswith("https://"):
            raise ValueError(f"URL must use HTTPS to protect credentials and privacy: {url}")
    return url
