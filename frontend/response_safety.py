"""Display-only credential filtering, not a validator of source evidence.

Technical prose is not metadata: words like 'Secret' and long resource IDs alone
are not credentials. Detection is intentionally limited to credential-bearing
syntax and recognizable token formats. No matched text is ever logged.
"""
from dataclasses import dataclass
import json
import logging
import re
import unicodedata


LOGGER = logging.getLogger(__name__)
FILTER_IMPLEMENTATION = "frontend.response_safety.filter_response"
FILTER_REVISION = "targeted-credentials-v1"
REDACTED = "[REDACTED]"
NOTICE = (
    "**Display redaction:** Credential values were removed. This display is not "
    "an exact source excerpt; backend evidence and validation are unchanged.\n\n"
)
WITHHELD = (
    "The final response was withheld because credential-bearing or unsafe content "
    "could not be safely isolated for redaction. No sensitive diagnostic details "
    "are displayed, and no redacted content is presented as exact evidence."
)

_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])[\"'`*]*(?:(?:[a-z0-9]+[_-])*"
    r"(?:password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|client[_-]key[_-]data|secret[_-]?access[_-]?key|accountkey|sharedaccesssignature|"
    r"token|secret)|connection[_ -]?string|authorization)"
    r"[\"'`*]*[ \t]*[:=][ \t]*"
)
_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_URL_CREDENTIAL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://(?P<value>[^\s/@:]+:[^\s/@]+)@")
_SIGNED_QUERY = re.compile(r"(?i)[?&](?:sig|token|access_token|api_key|client_secret)=(?P<value>[^\s&#\"'<>`]+)")
_HEADER = re.compile(r"(?im)\b(?:Authorization|Proxy-Authorization)[\"'`*]*\s*[:=]\s*(?P<value>[^\r\n]+)")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_SECRET_OBJECT = re.compile(r"(?i)[\"']?kind[\"']?\s*:\s*[\"']?Secret(?:[\"'\s,}]|$)")
_SECRET_DATA = re.compile(r"(?i)(?<!\w)[\"']?(?:data|stringData)[\"']?\s*:")
_REFERENCE = re.compile(r"(?:var|local)\.[\w.-]+|\$\{[^{}\r\n]+\}|\$\([^()\r\n]+\)")
_MASKED = re.compile(r"\[redacted\]", re.IGNORECASE)


@dataclass(frozen=True, repr=False)
class DisplayResponse:
    text: str
    outcome: str
    categories: tuple[str, ...]


def log_loaded_filter():
    """Identify the loaded implementation without inspecting any request data."""
    try:
        LOGGER.info(json.dumps({
            "event": "frontend_response_safety_loaded",
            "implementation": FILTER_IMPLEMENTATION,
            "revision": FILTER_REVISION,
        }, sort_keys=True))
    except Exception:
        pass


def _legacy_categories(text):
    """Explain the retired broad gate using fixed categories, never matched text."""
    categories = []
    if re.search(r"(?i)password|secret|token|credential|authorization|api.?key|stringdata", text):
        categories.append("legacy_keyword")
    if "://" in text:
        categories.append("legacy_url")
    if re.search(r"[A-Za-z0-9+/=_-]{40,}", text):
        categories.append("legacy_long_identifier")
    return categories


def filter_response(text):
    """Return a display copy. Fail closed on ambiguous credential boundaries."""
    categories = set()
    spans = []
    normalized = unicodedata.normalize("NFKC", text)

    def finish(outcome, output):
        result = DisplayResponse(output, outcome, tuple(sorted(categories)))
        try:
            LOGGER.info(json.dumps({
                "event": "frontend_response_filter", "outcome": outcome,
                "implementation": FILTER_IMPLEMENTATION, "revision": FILTER_REVISION,
                "display_action": "displayed" if outcome == "allowed" else outcome,
                "categories": result.categories,
                "legacy_triggers": _legacy_categories(normalized),
            }, sort_keys=True))
        except Exception:
            pass  # Logging failure must not affect filtering or expose content.
        return result

    if any(unicodedata.category(c).startswith("C") and c not in "\n\r\t" for c in normalized):
        categories.add("unsafe_control_character")
        return finish("withheld", WITHHELD)
    if _PRIVATE_KEY.search(normalized):
        categories.add("private_key_block")
        return finish("withheld", WITHHELD)
    if _SECRET_OBJECT.search(normalized) and _SECRET_DATA.search(normalized):
        categories.add("kubernetes_secret_data")
        return finish("withheld", WITHHELD)

    for pattern, category in ((_TOKENS, "credential_token"), (_URL_CREDENTIAL, "url_credentials"),
                              (_SIGNED_QUERY, "signed_query"), (_HEADER, "authorization_header")):
        for match in pattern.finditer(normalized):
            categories.add(category)
            spans.append(match.span("value") if "value" in pattern.groupindex else match.span())

    for match in _ASSIGNMENT.finditer(normalized):
        start = match.end()
        # Existing redaction and syntactic Terraform/pipeline references are safe.
        rest = normalized[start:]
        if re.match(r"(?i)\[redacted\](?=$|[\s,;}])", rest):
            continue
        if not rest or rest[0] in "\r\n|>{[":
            categories.add("ambiguous_credential_value")
            return finish("withheld", WITHHELD)
        if rest[0] in "\"'`":
            quote = rest[0]
            value_match = re.match(r"(?:\\.|[^\\" + re.escape(quote) + r"\r\n])*" + re.escape(quote), rest[1:])
            if value_match is None:
                categories.add("ambiguous_credential_value")
                return finish("withheld", WITHHELD)
            start += 1
            end = start + value_match.end() - 1
            if normalized[end + 1:end + 2] and normalized[end + 1] not in " \t\r\n,;}):.*":
                categories.add("ambiguous_credential_value")
                return finish("withheld", WITHHELD)
        else:
            # For unquoted multiword credentials, redact to end of field/line,
            # not just the first word. Never leave a credential suffix visible.
            value_match = re.match(r"[^\r\n]+", rest)
            if value_match is None:
                categories.add("ambiguous_credential_value")
                return finish("withheld", WITHHELD)
            end = start + len(value_match.group().rstrip())
        value = normalized[start:end]
        if not value or _MASKED.fullmatch(value) or _REFERENCE.fullmatch(value):
            continue
        categories.add("credential_assignment")
        spans.append((start, end))

    if not spans:
        return finish("allowed", text)
    # Normalization may change offsets. Do not apply those offsets to source text.
    if normalized != text:
        categories.add("normalized_credential")
        return finish("withheld", WITHHELD)
    # Remove repeated copies of detected values, even if an echo lacks a label.
    values = {text[start:end] for start, end in spans}
    for value in values:
        spans.extend(m.span() for m in re.finditer(re.escape(value), text))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    output = text
    for start, end in reversed(merged):
        output = output[:start] + REDACTED + output[end:]
    return finish("redacted", NOTICE + output)
