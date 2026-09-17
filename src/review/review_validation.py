"""Validate review excerpts against successful file reads from this turn only."""
import json
import posixpath
import re

from langchain_core.messages import AIMessage, ToolMessage


REVIEW_FORMAT = """
For every infrastructure review, return ONLY a JSON object with a nonempty
"findings" array. Each finding must have these string keys:
"Finding", "Evidence", "File", "Why it matters", "Verification needed", "Confidence".
Evidence must be one short, exact, contiguous Terraform excerpt copied from a
get_content result in THIS question's run, preserving whitespace. No backticks,
ellipses, or explanation inside Evidence. File must be its repository path.
Read files again if only earlier conversation turns contain them.
Confidence must be "Inference" for recommendations or inferred risks. Code
matching confirms only the excerpt, never deployment state or service semantics.
Verification needed must name the extra facts needed to assess the recommendation.
Do not claim outage, internet reachability, or provider behavior as established
solely from a configuration value. Non-review answers may remain plain text.
"""


def file_path(path):
    return posixpath.normpath('/' + path.lstrip('/'))


def retrieved_files(messages):
    calls = {}
    files = {}
    for message in messages:
        if isinstance(message, AIMessage):
            calls.update({call['id']: call for call in message.tool_calls})
        if not isinstance(message, ToolMessage) or message.status == 'error':
            continue
        call = calls.get(message.tool_call_id, {})
        args = call.get('args', {})
        if call.get('name') != 'repo_file' or args.get('action') != 'get_content':
            continue
        path = args.get('path', '')
        if not path.endswith('.tf'):
            continue
        blocks = message.content
        if isinstance(blocks, str):
            blocks = [{'type': 'text', 'text': blocks}]
        texts = []
        for block in blocks:
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            text = block['text']
            # Remove only the server's outer untrusted-content envelope.
            match = re.fullmatch(r'<<([^>]+)>>[^\n]*\n([\s\S]*)\n<</\1>>', text)
            texts.append(match.group(2) if match else text)
        if texts:
            files[file_path(path)] = '\n'.join(texts)
    return files


def validate_review(content, files):
    """Fail closed on malformed output; never promote interpretations to facts."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        return [], [f'Review rejected: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}.']
    except (TypeError, ValueError) as error:
        return [], [f'Review rejected: invalid JSON input: {error}.']
    if not isinstance(payload, dict):
        return [], ['Review rejected: expected a JSON object containing a findings array.']
    if 'findings' not in payload:
        return [], ['Review rejected: missing findings array.']
    findings = payload['findings']
    if not isinstance(findings, list):
        return [], ['Review rejected: findings must be an array.']
    if not findings:
        return [], ['Review rejected: wrong number of findings: got 0; expected at least 1.']
    accepted, rejected = [], []
    fields = ('Finding', 'Evidence', 'File', 'Why it matters', 'Verification needed', 'Confidence')
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            rejected.append(f'Finding {index} rejected: expected an object.')
            continue
        missing = [key for key in fields if key not in finding]
        invalid = [key for key in fields if key in finding and (
            not isinstance(finding[key], str) or not finding[key].strip()
        )]
        if missing or invalid:
            reasons = []
            if missing:
                reasons.append('missing required field(s): ' + ', '.join(missing))
            if invalid:
                reasons.append('required field(s) must be nonempty strings: ' + ', '.join(invalid))
            rejected.append(f'Finding {index} rejected: ' + '; '.join(reasons) + '.')
            continue
        evidence = finding['Evidence']
        path = file_path(finding['File'])
        if path not in files:
            rejected.append(f'Finding {index} rejected: referenced file was not read this turn: {path}.')
            continue
        if len(evidence) > 1000:
            rejected.append(f'Finding {index} rejected: evidence excerpt exceeds 1000 characters (got {len(evidence)}).')
            continue
        if evidence not in files[path]:
            rejected.append(f'Finding {index} rejected: Evidence excerpt not found in {path}\nEvidence: {evidence!r}')
            continue
        item = dict(finding)
        item['File'] = path
        # A substring check cannot validate semantics, risk, or a recommendation.
        item['Confidence'] = 'Inference'
        item['Verification needed'] += (
            ' Repository excerpt verified only; validate the interpretation against '
            'provider documentation, deployed controls, and workload requirements.'
        )
        accepted.append(item)
    return accepted, rejected


def render_review(content, messages):
    files = retrieved_files(messages)
    findings, rejected = validate_review(content, files)
    lines = ['Validation files: ' + ', '.join(sorted(files))]
    lines.extend(rejected)
    for finding in findings:
        lines.append('')
        for key in ('Finding', 'Evidence', 'File', 'Why it matters', 'Verification needed', 'Confidence'):
            value = finding[key]
            if key == 'Evidence':
                value = f'Confirmed exact excerpt: {value!r}'
            if key == 'Why it matters':
                value = 'Unverified interpretation: ' + value
            lines.append(f'{key}: {value}')
    if not findings:
        lines.append('No evidence-validated findings to display.')
    return '\n'.join(lines)
