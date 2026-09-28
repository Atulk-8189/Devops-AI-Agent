"""Formatting utilities to render structured JSON and Markdown responses with visual hierarchy
while preserving exact content. All formatting is deterministic — no LLM calls are made here.
"""
import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# Pattern constants
# ---------------------------------------------------------------------------

_CMD_PATTERN = re.compile(
    r"""(?x)
    (?:
        [`"'](?P<quoted>(?:kubectl|az|terraform|git|helm|curl|docker)\b[^`"'\n]+)[`"']
        |
        (?:^|[\r\n])\s*(?:Run|Execute)?\s*[:]?\s*[`"']?(?P<line>(?:kubectl|az|terraform|git|helm|curl|docker)\s+[^\r\n`"']+)
    )
    """
)

# Markdown structure detectors
_MD_HEADING    = re.compile(r"^#{1,6}\s+\S",        re.MULTILINE)
_MD_CODE_FENCE = re.compile(r"^```",                 re.MULTILINE)
_MD_BULLET     = re.compile(r"^[\s]*[-*+]\s+\S",     re.MULTILINE)
_MD_NUMBERED   = re.compile(r"^\d+\.\s+\S",          re.MULTILINE)
_MD_TABLE      = re.compile(r"^\|.+\|",              re.MULTILINE)

# Terraform discovery metadata block — first line of Terraform workflow output
_TF_DISCOVERY_LINE = re.compile(
    r"^Terraform discovery:\s*(\{.*?\})\s*$",
    re.MULTILINE | re.DOTALL,
)

# Finding separator in plain-text Terraform review output
# Findings start with "Finding: <title>"
_TF_FINDING_BLOCK_START = re.compile(r"^Finding:\s*", re.MULTILINE)

# Evidence excerpt with Python repr quoting: 'text\n  more' or "text\n more"
_EVIDENCE_REPR_PATTERN = re.compile(r"^Confirmed exact excerpt:\s*(['\"])([\s\S]*?)\1\s*$")

# Azure DevOps pipeline discovery pattern
_ADO_DISCOVERY_LINE = re.compile(
    r"^Pipeline discovery:\s*(\{.*?\})\s*$",
    re.MULTILINE | re.DOTALL,
)

# Response type detection patterns
_TERRAFORM_SIGNAL = re.compile(
    r"""(?x)
    (?:Terraform\s+discovery:|
       resource\s+"[^"]+"\s+"[^"]+"\s*\{|
       module\s+"[^"]+"\s*\{|
       variable\s+"[^"]+"\s*\{|
       output\s+"[^"]+"\s*\{|
       provider\s+"\S+"\s*\{|
       (?:\.tf|terraform)\b)
    """,
    re.IGNORECASE,
)
_KUBECTL_SIGNAL = re.compile(
    r"\bkubectl\b|\baks\b|\bk8s\b|\bkubernetes\b|\bpod\b.*\bstatus\b",
    re.IGNORECASE,
)
_AZURE_SIGNAL  = re.compile(r"\baz\s+\w|\bAzure\b|\bResource\s+Group\b|\bSubscription\b", re.IGNORECASE)
_ADO_SIGNAL    = re.compile(r"\bAzure\s+DevOps\b|\bpipeline\b|\brelease\b|\brepository\b|\bbranch\b", re.IGNORECASE)
_ERROR_SIGNAL  = re.compile(r"\bError\b|\bException\b|\bFailed\b|\bfailure\b|\btraceback\b", re.IGNORECASE)

_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "terraform":  ("🏗️", "Terraform Review"),
    "kubernetes": ("☸️", "Kubernetes / AKS Diagnostics"),
    "ado":        ("🔄", "Azure DevOps Investigation"),
    "azure":      ("☁️", "Azure Resource Investigation"),
    "error":      ("🔴", "Error / Troubleshooting"),
    "general":    ("💬", "DevOps Response"),
}

# Executive summary detection - first paragraph or summary-like content
_EXEC_SUMMARY_PATTERN = re.compile(
    r"^(?P<summary>.{50,500}?)(?:\n\n|\n#|\n-|\n\d+\.|$)",
    re.MULTILINE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Public helpers (also used by tests)
# ---------------------------------------------------------------------------

def is_structured_json(text: str) -> bool:
    """Return True if *text* is a valid, complete JSON object or array."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not (
        (stripped.startswith("{") and stripped.endswith("}")) or
        (stripped.startswith("[") and stripped.endswith("]"))
    ):
        return False
    try:
        data = json.loads(text)
        return isinstance(data, (dict, list))
    except (ValueError, TypeError, RecursionError):
        return False


def extract_command(text: str) -> str | None:
    """Extract a shell command from instruction text without duplicating code blocks."""
    if not text or "```" in text:
        return None
    match = _CMD_PATTERN.search(text)
    if match:
        cmd = match.group("quoted") or match.group("line")
        if cmd:
            return cmd.strip()
    return None


def format_command_instruction(instruction: str) -> str:
    """Wrap extracted shell commands in copyable code blocks."""
    instruction = str(instruction).strip()
    if "```" in instruction:
        return instruction
    cmd = extract_command(instruction)
    if cmd:
        return f"{instruction}\n\n```sh\n{cmd}\n```"
    return instruction


def format_summary(value: Any) -> str:
    """Render a summary field as readable text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value)
    if isinstance(value, dict):
        return "\n".join(f"- **{k}**: {v}" for k, v in value.items())
    return str(value)


def format_observed_evidence(evidence: Any) -> str:
    """Render observed evidence as a structured list."""
    if isinstance(evidence, list):
        if not evidence:
            return "_No observed evidence reported._"
        lines = []
        for item in evidence:
            if isinstance(item, dict):
                sublines = [f"- **{k}**: {v}" for k, v in item.items()]
                lines.append("\n  ".join(sublines))
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)
    if isinstance(evidence, str):
        return evidence.strip()
    return str(evidence)


def format_likely_root_causes(causes: Any) -> str:
    """Render hypotheses as blockquote cards with supporting evidence."""
    if isinstance(causes, list):
        if not causes:
            return "_No likely root causes identified._"
        cards = []
        for i, item in enumerate(causes, start=1):
            if isinstance(item, dict):
                title = (
                    item.get("hypothesis") or item.get("Hypothesis") or
                    item.get("root_cause") or item.get("Root cause") or
                    item.get("cause") or f"Hypothesis {i}"
                )
                evidence = (
                    item.get("supporting_evidence") or item.get("Supporting evidence") or
                    item.get("evidence") or item.get("Evidence")
                )
                card_lines = [f"> #### Hypothesis {i}: {title}"]
                skip = {"hypothesis", "Hypothesis", "root_cause", "Root cause", "cause",
                        "supporting_evidence", "Supporting evidence", "evidence", "Evidence"}
                for k, v in item.items():
                    if k not in skip:
                        card_lines.append(f"> **{k}**: {v}")
                if evidence:
                    card_lines.append(">")
                    card_lines.append("> **Supporting evidence:**")
                    if isinstance(evidence, list):
                        for ev in evidence:
                            card_lines.append(f"> - {ev}")
                    else:
                        card_lines.append(f"> - {evidence}")
                cards.append("\n".join(card_lines))
            elif isinstance(item, str):
                cards.append(f"> #### Hypothesis {i}\n> {item}")
            else:
                cards.append(f"> #### Hypothesis {i}\n> {item}")
        return "\n\n".join(cards)
    if isinstance(causes, str):
        return f"> {causes.strip()}"
    return str(causes)


def format_generic_value(value: Any, indent_level: int = 0) -> str:
    """Recursively render arbitrary values as Markdown without data loss."""
    pad = "  " * indent_level
    if isinstance(value, str):
        return f"{pad}{value}"
    if isinstance(value, (int, float, bool)) or value is None:
        return f"{pad}`{json.dumps(value)}`"
    if isinstance(value, list):
        if not value:
            return f"{pad}_(empty list)_"
        lines = []
        for item in value:
            if isinstance(item, dict):
                sub = format_generic_value(item, indent_level + 1)
                lines.append(f"{pad}-\n{sub}")
            elif isinstance(item, (str, int, float, bool)) or item is None:
                lines.append(f"{pad}- {item if isinstance(item, str) else json.dumps(item)}")
            else:
                lines.append(f"{pad}- {json.dumps(item)}")
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return f"{pad}_(empty object)_"
        lines = []
        for k, v in value.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                lines.append(f"{pad}- **{k}**: {v if isinstance(v, str) else json.dumps(v)}")
            else:
                formatted_v = format_generic_value(v, indent_level + 1)
                lines.append(f"{pad}- **{k}**:\n{formatted_v}")
        return "\n".join(lines)
    return f"{pad}{value}"


def format_review_findings(findings: list[dict[str, Any]]) -> str:
    """Render structured JSON review findings as clearly-titled cards."""
    cards = []
    for i, f in enumerate(findings, start=1):
        if not isinstance(f, dict):
            cards.append(f"- {f}")
            continue
        title = f.get("Finding", f"Finding {i}")
        card_lines = [f"#### {i}. {title}"]
        file_path = f.get("File")
        line_num  = f.get("Evidence line") or f.get("evidence_line")
        end_line  = f.get("Evidence end line") or f.get("evidence_end_line")
        if file_path:
            location = f"`{file_path}`"
            if line_num:
                location += f" (line {line_num}" + (
                    f"-{end_line})" if end_line and end_line != line_num else ")"
                )
            card_lines.append(f"**Location:** {location}")
        why = f.get("Why it matters") or f.get("why_it_matters")
        if why:
            card_lines.append(f"**Why it matters:** {why}")
        verification = f.get("Verification needed") or f.get("verification_needed")
        if verification:
            card_lines.append(f"**Verification needed:** {verification}")
        confidence = f.get("Confidence") or f.get("confidence")
        if confidence:
            card_lines.append(f"**Confidence:** {confidence}")
        evidence = f.get("Evidence") or f.get("evidence")
        if evidence:
            lang = _guess_code_language(str(evidence))
            card_lines.append(f"**Evidence:**\n```{lang}\n{evidence}\n```")
        # Any remaining keys
        handled = {
            "Finding", "File", "Evidence line", "evidence_line", "Evidence end line",
            "evidence_end_line", "Why it matters", "why_it_matters",
            "Verification needed", "verification_needed", "Confidence", "confidence",
            "Evidence", "evidence",
        }
        for k, v in f.items():
            if k not in handled:
                card_lines.append(f"**{k}:** {v}")
        cards.append("\n\n".join(card_lines))
    return "\n\n---\n\n".join(cards)


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _find_key(data: dict[str, Any], candidates: list[str]) -> str | None:
    """Case-insensitive key lookup with normalisation."""
    for c in candidates:
        if c in data:
            return c
    normalized_map = {re.sub(r"[\s_-]+", "", k).lower(): k for k in data.keys()}
    for c in candidates:
        norm_c = re.sub(r"[\s_-]+", "", c).lower()
        if norm_c in normalized_map:
            return normalized_map[norm_c]
    return None


def _has_markdown_structure(text: str) -> bool:
    """Return True when the text contains meaningful Markdown structure."""
    return bool(
        _MD_HEADING.search(text) or
        _MD_CODE_FENCE.search(text) or
        (_MD_BULLET.search(text) and len(text) > 100) or
        (_MD_NUMBERED.search(text) and len(text) > 100) or
        _MD_TABLE.search(text)
    )


def _detect_response_type(text: str) -> str:
    """Detect the primary response type from content signals. More-specific checks first."""
    if _TERRAFORM_SIGNAL.search(text):
        return "terraform"
    if _KUBECTL_SIGNAL.search(text):
        return "kubernetes"
    if _ADO_SIGNAL.search(text):
        return "ado"
    if _AZURE_SIGNAL.search(text):
        return "azure"
    if _ERROR_SIGNAL.search(text):
        return "error"
    return "general"


def _guess_code_language(text: str) -> str:
    """Pick a syntax-highlight language for a code fence based on content heuristics."""
    stripped = text.strip()
    if re.search(r"^\s*resource\s+\"", stripped, re.MULTILINE) or stripped.endswith(".tf"):
        return "hcl"
    if re.search(r"^\s*(apiVersion|kind|metadata|spec):", stripped, re.MULTILINE):
        return "yaml"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if re.search(r"\$\s+\w", stripped) or stripped.startswith("kubectl") or stripped.startswith("az "):
        return "sh"
    return ""


def _extract_executive_summary(text: str) -> str | None:
    """Extract a concise executive summary from the beginning of a response."""
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    if len(stripped) < 50:
        return None
    
    # Try to find a summary-like first paragraph
    match = _EXEC_SUMMARY_PATTERN.search(stripped)
    if match:
        summary = match.group("summary").strip()
        # Clean up - remove any leading markers
        summary = re.sub(r"^[#\-\*\d\.\s]+", "", summary).strip()
        if len(summary) >= 50 and len(summary) <= 500:
            return summary
    
    # Fallback: first 300 chars of first paragraph
    first_para = stripped.split("\n\n")[0]
    if len(first_para) >= 50:
        return first_para[:300].strip() + ("..." if len(first_para) > 300 else "")
    
    return None


def _make_original_block(original: str, label: str = "Original Response") -> str:
    """Return a collapsible <details> block preserving the full original text."""
    safe = original.replace("</details>", "<\\/details>")
    return (
        f"<details>\n"
        f"<summary><strong>{label}</strong></summary>\n\n"
        f"{safe}\n"
        f"</details>"
    )


def _make_raw_json_block(raw_json: str) -> str:
    """Return a collapsible <details> block for raw JSON."""
    content = raw_json if raw_json.endswith("\n") else f"{raw_json}\n"
    return (
        f"<details>\n"
        f"<summary><strong>Raw response</strong></summary>\n\n"
        f"```json\n"
        f"{content}"
        f"```\n"
        f"</details>"
    )


def _render_discovery_metadata_table(discovery: dict) -> str:
    """Render Terraform discovery metadata as a compact Markdown table."""
    if not isinstance(discovery, dict):
        return ""
    # Select and order interesting keys
    priority = ["repository_id", "commit_sha", "commit_id", "branch", "project",
                 "incomplete", "error", "files_read", "repository_name"]
    rows = []
    seen = set()
    for key in priority:
        if key in discovery:
            rows.append((key, discovery[key]))
            seen.add(key)
    for key, val in discovery.items():
        if key not in seen:
            rows.append((key, val))
    if not rows:
        return ""
    lines = ["| Field | Value |", "|---|---|"]
    for key, val in rows:
        if isinstance(val, bool):
            display = "Yes" if val else "No"
        elif val is None:
            display = "—"
        elif isinstance(val, (dict, list)):
            display = f"`{json.dumps(val)}`"
        else:
            display = str(val)
        lines.append(f"| `{key}` | {display} |")
    return "\n".join(lines)


def _decode_evidence_repr(raw: str) -> str:
    """
    Parse a Python repr-style evidence string produced by render_review().

    render_review() wraps evidence like:  Confirmed exact excerpt: 'foo\\n  bar'
    The single or double quotes are Python repr delimiters; \\n inside them is a
    literal backslash-n escape that should be rendered as a real newline.
    """
    raw = raw.strip()
    match = _EVIDENCE_REPR_PATTERN.match(raw)
    if match:
        # Extract the content between the repr quotes and decode \n, \t, \\
        inner = match.group(2)
        # Replace common Python string escapes; avoid eval()
        inner = inner.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        return inner
    # Fallback: if no repr wrapper, return as-is
    return raw


# ---------------------------------------------------------------------------
# Terraform plain-text formatter
# ---------------------------------------------------------------------------

def _parse_terraform_finding_block(block: str) -> dict[str, str]:
    """
    Parse one flat key-value finding block into a dict.

    The format from render_review() is:
        Finding: <title>
        Evidence: Confirmed exact excerpt: '<code>'
        File: /terraform/main.tf
        Why it matters: Unverified interpretation: <text>
        Verification needed: <text>
        Confidence: Inference
    """
    result: dict[str, str] = {}
    # Keys that may contain multi-line values (Evidence repr may span lines)
    field_names = [
        "Finding", "Evidence", "File",
        "Why it matters", "Verification needed", "Confidence",
    ]
    # Build a pattern that splits on "^FieldName: " anchors
    pattern = re.compile(
        r"^(" + "|".join(re.escape(f) for f in field_names) + r"):\s*",
        re.MULTILINE,
    )
    parts = pattern.split(block)
    # parts = ['', 'Finding', 'value...', 'Evidence', 'value...', ...]
    it = iter(parts)
    next(it, None)  # skip leading empty string
    while True:
        key = next(it, None)
        val = next(it, None)
        if key is None:
            break
        result[key] = val.strip() if val else ""
    return result


def _format_terraform_finding_card(idx: int, fields: dict[str, str]) -> str:
    """Render a single Terraform finding as a styled Markdown card."""
    title = fields.get("Finding", f"Finding {idx}")
    file_path = fields.get("File", "")
    raw_evidence = fields.get("Evidence", "")
    why = fields.get("Why it matters", "")
    verification = fields.get("Verification needed", "")
    confidence = fields.get("Confidence", "")

    lines: list[str] = [f"#### {idx}. {title}"]

    if file_path:
        lines.append(f"**File:** `{file_path}`")

    # Determine where "Unverified interpretation:" prefix ends
    if why.startswith("Unverified interpretation:"):
        why_clean = why[len("Unverified interpretation:"):].strip()
        lines.append(f"**Why it matters:** _{why_clean}_")
        lines.append("> ⚠️ _Unverified interpretation — confirm against provider documentation and deployed state._")
    elif why:
        lines.append(f"**Why it matters:** {why}")

    if confidence:
        badge = "🟡 Inference" if "inference" in confidence.lower() else confidence
        lines.append(f"**Confidence:** {badge}")

    if verification:
        # Strip the appended "Repository excerpt verified only; ..." boilerplate
        # so the user sees a clean verification step
        suffix_marker = " Repository excerpt verified only;"
        clean_v = verification
        if suffix_marker in clean_v:
            clean_v = clean_v[:clean_v.index(suffix_marker)].strip()
            remainder = verification[verification.index(suffix_marker):]
            lines.append(f"**Verification needed:** {clean_v}")
            lines.append(
                f"<details>\n<summary>Scope caveat</summary>\n\n"
                f"_{remainder.strip()}_\n</details>"
            )
        else:
            lines.append(f"**Verification needed:** {clean_v}")

    if raw_evidence:
        evidence_text = _decode_evidence_repr(raw_evidence)
        lang = _guess_code_language(evidence_text)
        lines.append(f"**Evidence (source excerpt):**\n```{lang}\n{evidence_text}\n```")

    return "\n\n".join(lines)


def _format_terraform_review_text(text: str) -> str:
    """
    Format the plain-text Terraform review output produced by handle_terraform().

    Structure of the raw text:
        Terraform discovery: {<json>}
        Validation files: a.tf, b.tf
        [Finding: ...\nEvidence: ...\n...]* OR 'No evidence-validated findings to display.'
    """
    original_block = _make_original_block(text)

    # ── 1. Extract discovery metadata ──────────────────────────────────────
    discovery_match = _TF_DISCOVERY_LINE.search(text)
    discovery_table = ""
    if discovery_match:
        try:
            discovery_data = json.loads(discovery_match.group(1))
            discovery_table = _render_discovery_metadata_table(discovery_data)
        except (json.JSONDecodeError, ValueError):
            discovery_table = f"`{discovery_match.group(1)}`"

    # Remove the raw discovery line from what we'll parse further
    remainder = _TF_DISCOVERY_LINE.sub("", text).strip()

    # ── 2. Extract validation files line ──────────────────────────────────
    val_files_line = ""
    val_files_match = re.search(r"^(Validation files:.*)$", remainder, re.MULTILINE)
    if val_files_match:
        val_files_line = val_files_match.group(1)
        remainder = remainder[:val_files_match.start()] + remainder[val_files_match.end():]
        remainder = remainder.strip()

    # ── 3. Parse individual finding blocks ────────────────────────────────
    # Split on blank lines between findings
    raw_finding_blocks: list[str] = []
    # Findings start at a "Finding: " line
    finding_positions = [m.start() for m in _TF_FINDING_BLOCK_START.finditer(remainder)]
    if finding_positions:
        for idx, start in enumerate(finding_positions):
            end = finding_positions[idx + 1] if idx + 1 < len(finding_positions) else len(remainder)
            raw_finding_blocks.append(remainder[start:end].strip())
    else:
        # No findings found — keep remainder as a note block
        pass

    # ── 4. Assemble sections ───────────────────────────────────────────────
    sections: list[str] = []

    # Header
    sections.append("### 🏗️ Terraform Review")

    # Discovery metadata table
    if discovery_table:
        sections.append(
            f"#### 📦 Repository Discovery\n\n{discovery_table}"
        )

    # Validation files
    if val_files_line:
        files_list = val_files_line.replace("Validation files:", "").strip()
        files_md = "\n".join(f"- `{f.strip()}`" for f in files_list.split(",") if f.strip())
        sections.append(f"#### 📁 Files Reviewed\n\n{files_md}")

    # Findings
    if raw_finding_blocks:
        finding_cards = []
        for i, raw_block in enumerate(raw_finding_blocks, start=1):
            fields = _parse_terraform_finding_block(raw_block)
            if fields:
                finding_cards.append(_format_terraform_finding_card(i, fields))
            else:
                finding_cards.append(f"#### {i}. (unparsed finding)\n\n```\n{raw_block}\n```")
        sections.append(f"#### 🔎 Findings ({len(finding_cards)})\n\n" + "\n\n---\n\n".join(finding_cards))
    else:
        # Check for "no findings" message in remainder
        if "no evidence-validated findings" in remainder.lower() or "no findings" in remainder.lower():
            sections.append("#### 🔎 Findings\n\n_No evidence-validated findings to display._")
        elif remainder.strip():
            sections.append(f"#### Notes\n\n{remainder.strip()}")

    body = "\n\n".join(sections)
    return f"{body}\n\n---\n\n{original_block}"


# ---------------------------------------------------------------------------
# Azure DevOps Pipeline formatter
# ---------------------------------------------------------------------------

def _format_ado_pipeline_text(text: str) -> str:
    """
    Format Azure DevOps pipeline investigation output.

    Structure of the raw text:
        Pipeline discovery: {<json>}
        [Pipeline: <name>\nStatus: <status>\n...]* or JSON response
    """
    original_block = _make_original_block(text)

    # ── 1. Extract discovery metadata ──────────────────────────────────────
    discovery_match = _ADO_DISCOVERY_LINE.search(text)
    discovery_table = ""
    if discovery_match:
        try:
            discovery_data = json.loads(discovery_match.group(1))
            discovery_table = _render_discovery_metadata_table(discovery_data)
        except (json.JSONDecodeError, ValueError):
            discovery_table = f"`{discovery_match.group(1)}`"

    # Remove the raw discovery line from what we'll parse further
    remainder = _ADO_DISCOVERY_LINE.sub("", text).strip()

    # ── 2. Try to parse as JSON first ──────────────────────────────────────
    if is_structured_json(remainder):
        # Use the JSON formatter for structured pipeline data
        json_formatted = _format_json_response(remainder)
        # Prepend discovery table if available
        if discovery_table:
            header = "### 🔄 Azure DevOps Pipeline Investigation"
            body = f"{header}\n\n#### 📦 Pipeline Discovery\n\n{discovery_table}\n\n" + json_formatted.split("\n\n", 1)[1] if "\n\n" in json_formatted else f"{header}\n\n{discovery_table}"
            return f"{body}\n\n---\n\n{original_block}"
        return json_formatted + f"\n\n---\n\n{original_block}"

    # ── 3. Parse pipeline information blocks ──────────────────────────────
    # Look for pipeline name, status, stages, etc.
    pipeline_blocks: list[str] = []
    
    # Try to find pipeline sections
    pipeline_start_pattern = re.compile(r"^(Pipeline|Repository|Branch|Stage|Job):\s*", re.MULTILINE)
    pipeline_positions = [m.start() for m in pipeline_start_pattern.finditer(remainder)]
    
    if pipeline_positions:
        for idx, start in enumerate(pipeline_positions):
            end = pipeline_positions[idx + 1] if idx + 1 < len(pipeline_positions) else len(remainder)
            pipeline_blocks.append(remainder[start:end].strip())
    elif remainder:
        pipeline_blocks.append(remainder)

    # ── 4. Assemble sections ───────────────────────────────────────────────
    sections: list[str] = []

    # Header
    sections.append("### 🔄 Azure DevOps Pipeline Investigation")

    # Discovery metadata table
    if discovery_table:
        sections.append(
            f"#### 📦 Pipeline Discovery\n\n{discovery_table}"
        )

    # Pipeline details
    if pipeline_blocks:
        pipeline_cards = []
        for i, raw_block in enumerate(pipeline_blocks, start=1):
            card_lines = [f"#### {i}. Pipeline Details"]
            # Parse key-value pairs
            for line in raw_block.split("\n"):
                if ": " in line:
                    key, val = line.split(": ", 1)
                    card_lines.append(f"**{key}:** {val}")
                elif line.strip():
                    card_lines.append(line.strip())
            pipeline_cards.append("\n\n".join(card_lines))
        sections.append(f"#### 🔎 Pipeline Information\n\n" + "\n\n---\n\n".join(pipeline_cards))

    body = "\n\n".join(sections)
    return f"{body}\n\n---\n\n{original_block}"


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

def _format_json_response(text: str) -> str:
    """Format a valid JSON string into rich Markdown with a collapsible raw block."""
    try:
        data = json.loads(text)
    except Exception:
        return text

    raw_block = _make_raw_json_block(text)

    if isinstance(data, list):
        items_formatted = []
        if all(isinstance(x, dict) and "Finding" in x for x in data):
            items_formatted.append("### 🔎 Review Findings\n\n" + format_review_findings(data))
        else:
            for i, item in enumerate(data, start=1):
                items_formatted.append(f"#### Item {i}\n{format_generic_value(item)}")
        body = "\n\n".join(items_formatted)
        return f"{body}\n\n---\n\n{raw_block}"

    if not isinstance(data, dict):
        return text

    sections: list[str] = []
    handled: set[str] = set()

    # 0. Executive Summary (if present in data or extracted)
    exec_summary = None
    summary_key = _find_key(data, ["Summary", "summary", "Executive Summary", "executive_summary"])
    if summary_key:
        handled.add(summary_key)
        exec_summary = format_summary(data[summary_key])
    else:
        # Try to extract from the full text
        exec_summary = _extract_executive_summary(text)
    
    if exec_summary:
        sections.append(f"### 📋 Executive Summary\n{exec_summary}")

    # 1. Summary (if different from executive summary)
    if summary_key and exec_summary and exec_summary != format_summary(data[summary_key]):
        sections.append(f"### 📋 Summary\n{format_summary(data[summary_key])}")

    # 2. Observed evidence
    evidence_key = _find_key(data, ["Observed evidence", "observed_evidence", "Observed Evidence"])
    if evidence_key:
        handled.add(evidence_key)
        sections.append(f"### 🔍 Observed Evidence\n{format_observed_evidence(data[evidence_key])}")

    # 3. Likely root causes
    causes_key = _find_key(data, ["Likely root causes", "likely_root_causes", "Likely Root Causes"])
    if causes_key:
        handled.add(causes_key)
        sections.append(f"### ⚠️ Likely Root Causes\n{format_likely_root_causes(data[causes_key])}")

    # 4. Recommended next diagnostic step
    next_key = _find_key(data, [
        "Recommended next diagnostic step",
        "recommended_next_diagnostic_step",
        "Recommended Next Diagnostic Step",
    ])
    if next_key:
        handled.add(next_key)
        sections.append(
            f"### 💡 Recommended Next Diagnostic Step\n"
            f"{format_command_instruction(str(data[next_key]))}"
        )

    # 5. Findings list (e.g. proposal / remediation result)
    findings_key = _find_key(data, ["findings", "Findings"])
    if findings_key and findings_key not in handled:
        handled.add(findings_key)
        val = data[findings_key]
        if isinstance(val, list) and all(isinstance(x, dict) for x in val):
            sections.append(f"### 🔎 Findings\n\n{format_review_findings(val)}")
        else:
            sections.append(f"### 🔎 Findings\n\n{format_generic_value(val)}")

    # 6. Repository / Pipeline metadata - render as compact table
    metadata_keys = ["repository", "pipeline", "repository_id", "commit_sha", "commit_id", "branch", "project", "discovery"]
    for key in metadata_keys:
        if key in data and key not in handled:
            handled.add(key)
            val = data[key]
            if isinstance(val, dict):
                table = _render_discovery_metadata_table(val)
                if table:
                    sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{table}")
                else:
                    sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{format_generic_value(val)}")
            elif isinstance(val, str) and (val.startswith("{") or val.startswith("[")):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, dict):
                        table = _render_discovery_metadata_table(parsed)
                        if table:
                            sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{table}")
                        else:
                            sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{format_generic_value(val)}")
                    else:
                        sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{format_generic_value(val)}")
                except Exception:
                    sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{format_generic_value(val)}")
            else:
                sections.append(f"### 📦 {key.replace('_', ' ').title()}\n\n{format_generic_value(val)}")

    # 7. Remaining keys — render as expandable sections for large values
    for key, val in data.items():
        if key in handled:
            continue
        formatted_val = format_generic_value(val)
        # Put large / deeply nested values inside a collapsible block
        if len(formatted_val) > 400 or isinstance(val, (dict, list)):
            sections.append(
                f"<details>\n<summary><strong>{key}</strong></summary>\n\n"
                f"{formatted_val}\n</details>"
            )
        else:
            sections.append(f"### {key}\n{formatted_val}")

    if not sections:
        sections.append("### Response\n_No structured content found._")

    body = "\n\n".join(sections)
    return f"{body}\n\n---\n\n{raw_block}"


# ---------------------------------------------------------------------------
# Markdown / plain-text formatters
# ---------------------------------------------------------------------------

def _contextual_header(text: str) -> str:
    rtype = _detect_response_type(text)
    icon, label = _TYPE_LABELS.get(rtype, ("💬", "DevOps Response"))
    return f"### {icon} {label}"


def _format_markdown_response(text: str) -> str:
    """Wrap a Markdown-structured response with a contextual header, executive summary, and original block."""
    header = _contextual_header(text)
    original_block = _make_original_block(text)
    exec_summary = _extract_executive_summary(text)
    if exec_summary:
        return f"{header}\n\n### 📋 Executive Summary\n{exec_summary}\n\n{text}\n\n---\n\n{original_block}"
    return f"{header}\n\n{text}\n\n---\n\n{original_block}"


def _format_plain_response(text: str) -> str:
    """Wrap plain prose with a contextual header, executive summary, and an original block."""
    header = _contextual_header(text)
    original_block = _make_original_block(text)
    exec_summary = _extract_executive_summary(text)
    if exec_summary:
        return f"{header}\n\n### 📋 Executive Summary\n{exec_summary}\n\n{text}\n\n---\n\n{original_block}"
    return f"{header}\n\n{text}\n\n---\n\n{original_block}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def format_response(text: str) -> str:
    """
    Format a response for the Chainlit UI with visual hierarchy.

    Layered strategy (deterministic, no LLM calls):
      1. Valid JSON           → rich structured JSON formatter.
      2. Terraform review     → specialised plain-text finding-card renderer.
      3. Azure DevOps pipeline → specialised pipeline renderer.
      4. Markdown-structured  → contextual header + preserve + original block.
      5. Plain prose          → contextual header + preserve + original block.
    """
    if not isinstance(text, str):
        return str(text)

    # Layer 1: structured JSON
    if is_structured_json(text):
        return _format_json_response(text)

    # Layer 2: Terraform plain-text review output
    if _TERRAFORM_SIGNAL.search(text) and "Terraform discovery:" in text:
        return _format_terraform_review_text(text)

    # Layer 3: Azure DevOps pipeline output
    if _ADO_SIGNAL.search(text) and ("Pipeline discovery:" in text or "pipeline" in text.lower()):
        return _format_ado_pipeline_text(text)

    # Layer 4 & 5: Markdown or prose
    if _has_markdown_structure(text):
        return _format_markdown_response(text)

    return _format_plain_response(text)
