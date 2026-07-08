from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Iterable
from urllib.parse import urlparse

from config import AgentConfig, normalize_base_url


COOKIE_NAMES = {
    "session_token",
    "cf_clearance",
    "sensorsdata2015jssdkcross",
    "language",
}


@dataclass
class ParsedRequest:
    url: str = ""
    user_agent: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        if not self.url:
            return ""
        parsed = urlparse(self.url)
        if not parsed.scheme or not parsed.netloc:
            return ""
        scheme = parsed.scheme
        if scheme == "wss":
            scheme = "https"
        elif scheme == "ws":
            scheme = "http"
        return f"{scheme}://{parsed.netloc}"


def parse_request_text(text: str) -> ParsedRequest:
    text = text.strip()
    if not text:
        return ParsedRequest(notes=["未发现可识别内容"])
    if text.lower().lstrip().startswith("curl"):
        return parse_curl(text)
    if "Invoke-WebRequest" in text or "System.Net.Cookie" in text:
        return parse_powershell(text)
    parsed = parse_generic(text)
    if not parsed.url and not parsed.cookies and not parsed.headers:
        parsed.notes.append("未识别为 PowerShell 或 curl 请求")
    return parsed


def apply_parsed_request(config: AgentConfig, parsed: ParsedRequest) -> AgentConfig:
    if parsed.base_url:
        config.base_url = normalize_base_url(parsed.base_url)
    if parsed.user_agent:
        config.user_agent = parsed.user_agent
    if "session_token" in parsed.cookies:
        config.session_token = parsed.cookies["session_token"]
    if "cf_clearance" in parsed.cookies:
        config.cf_clearance = parsed.cookies["cf_clearance"]
    if "sensorsdata2015jssdkcross" in parsed.cookies:
        config.sensorsdata2015jssdkcross = parsed.cookies["sensorsdata2015jssdkcross"]
    if "language" in parsed.cookies:
        config.language = parsed.cookies["language"]

    known = {
        "session_token",
        "cf_clearance",
        "sensorsdata2015jssdkcross",
        "language",
    }
    extra = []
    existing = _cookie_header_to_dict(config.extra_cookies)
    for name, value in parsed.cookies.items():
        if name not in known:
            existing[name] = value
    for name, value in existing.items():
        if name and name not in known:
            extra.append(f"{name}={value}")
    config.extra_cookies = "; ".join(extra)
    return config


def parse_powershell(text: str) -> ParsedRequest:
    result = ParsedRequest()
    uri = _first_match(text, r'-Uri\s+["\']([^"\']+)["\']')
    if uri:
        result.url = uri

    ua = _first_match(text, r'\.UserAgent\s*=\s*["\']([^"\']+)["\']')
    if not ua:
        ua = _first_header_value(text, "user-agent")
    if ua:
        result.user_agent = ua

    for match in re.finditer(
        r'Cookie\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']*)["\']',
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        result.cookies[match.group(1)] = _unescape_ps(match.group(2))

    cookie_header = _first_header_value(text, "cookie")
    if cookie_header:
        result.cookies.update(_cookie_header_to_dict(cookie_header))

    for key, value in _parse_ps_headers(text).items():
        result.headers[key] = value
        if key.lower() == "cookie":
            result.cookies.update(_cookie_header_to_dict(value))
        elif key.lower() == "user-agent" and not result.user_agent:
            result.user_agent = value

    if not result.cookies:
        result.notes.append("未从 PowerShell 中找到 Cookie")
    return result


def parse_curl(text: str) -> ParsedRequest:
    result = ParsedRequest()
    try:
        tokens = shlex.split(_join_curl_lines(text), posix=False)
    except ValueError:
        tokens = _fallback_split(text)
        result.notes.append("curl 引号不完整，已使用宽松解析")

    tokens = [_strip_quote(token) for token in tokens]
    if tokens and tokens[0].lower() == "curl":
        tokens = tokens[1:]

    i = 0
    while i < len(tokens):
        token = tokens[i]
        low = token.lower()
        if low in {"-h", "--header", "-a", "--user-agent", "-b", "--cookie", "-e", "--referer"}:
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
            if low in {"-h", "--header"}:
                _consume_header(result, value)
            elif low in {"-a", "--user-agent"}:
                result.user_agent = value
            elif low in {"-b", "--cookie"}:
                result.cookies.update(_cookie_header_to_dict(value))
            continue
        if low.startswith("-h") and len(token) > 2:
            _consume_header(result, token[2:].strip())
        elif low.startswith("--header="):
            _consume_header(result, token.split("=", 1)[1])
        elif low.startswith("-a") and len(token) > 2:
            result.user_agent = token[2:].strip()
        elif low.startswith("--user-agent="):
            result.user_agent = token.split("=", 1)[1]
        elif low.startswith("--cookie="):
            result.cookies.update(_cookie_header_to_dict(token.split("=", 1)[1]))
        elif not token.startswith("-") and not result.url and _looks_like_url(token):
            result.url = token
        else:
            possible_url = _extract_url(token)
            if possible_url and not result.url:
                result.url = possible_url
        i += 1

    if not result.url:
        url = _extract_url(text)
        if url:
            result.url = url
    if not result.cookies:
        result.notes.append("未从 curl 中找到 Cookie")
    return result


def parse_generic(text: str) -> ParsedRequest:
    result = ParsedRequest()
    url = _extract_url(text)
    if url:
        result.url = url
    for header in re.findall(r'^\s*([^:\r\n]+)\s*:\s*(.+?)\s*$', text, re.MULTILINE):
        _consume_header(result, f"{header[0]}: {header[1]}")
    for name in COOKIE_NAMES:
        value = _first_match(text, rf'{re.escape(name)}["\']?\s*[:=]\s*["\']?([^"\'\s;]+)')
        if value:
            result.cookies[name] = value
    return result


def describe_parsed(parsed: ParsedRequest) -> str:
    lines = []
    if parsed.base_url:
        lines.append(f"面板地址: {parsed.base_url}")
    if parsed.user_agent:
        lines.append("User-Agent: 已识别")
    for name in ["session_token", "cf_clearance", "sensorsdata2015jssdkcross", "language"]:
        if name in parsed.cookies:
            lines.append(f"{name}: 已识别")
    extra = [name for name in parsed.cookies if name not in COOKIE_NAMES]
    if extra:
        lines.append(f"额外 Cookie: {', '.join(extra)}")
    lines.extend(parsed.notes)
    return "\n".join(lines) if lines else "没有识别到可填充字段"


def _consume_header(result: ParsedRequest, header: str) -> None:
    header = _strip_quote(header.strip())
    if ":" not in header:
        return
    key, value = header.split(":", 1)
    key = key.strip()
    value = value.strip()
    result.headers[key] = value
    low = key.lower()
    if low == "cookie":
        result.cookies.update(_cookie_header_to_dict(value))
    elif low == "user-agent":
        result.user_agent = value


def _parse_ps_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    block = _first_match(text, r'-Headers\s+@\{(.*?)\}', flags=re.IGNORECASE | re.DOTALL)
    if not block:
        return headers
    for match in re.finditer(
        r'["\']?([^"\'=\r\n]+?)["\']?\s*=\s*["\']((?:`.|[^"\'])*)["\']',
        block,
        re.DOTALL,
    ):
        key = match.group(1).strip()
        value = _unescape_ps(match.group(2).strip())
        if key:
            headers[key] = value
    return headers


def _first_header_value(text: str, header_name: str) -> str:
    headers = _parse_ps_headers(text)
    for key, value in headers.items():
        if key.lower() == header_name.lower():
            return value
    return ""


def _cookie_header_to_dict(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    try:
        parsed = SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            cookies[name] = morsel.value
        if cookies:
            return cookies
    except Exception:
        pass
    for chunk in header.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name:
            cookies[name] = value
    return cookies


def _join_curl_lines(text: str) -> str:
    return re.sub(r'\\\r?\n|`\r?\n|\^\r?\n', " ", text)


def _fallback_split(text: str) -> list[str]:
    return re.findall(r'"[^"]*"|\'[^\']*\'|\S+', _join_curl_lines(text))


def _strip_quote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _unescape_ps(value: str) -> str:
    return (
        value.replace('`"', '"')
        .replace("`'", "'")
        .replace("``", "`")
        .replace("`r", "\r")
        .replace("`n", "\n")
        .replace("`t", "\t")
    )


def _extract_url(text: str) -> str:
    match = re.search(r'(?:https?|wss?)://[^\s\'"`<>]+', text)
    return match.group(0).rstrip("\\,)") if match else ""


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "ws://", "wss://"))


def _first_match(text: str, pattern: str, flags: int = re.IGNORECASE) -> str:
    match = re.search(pattern, text, flags)
    return match.group(1) if match else ""
