"""Extract top-level Terraform block structure without evaluating expressions."""

import json

import hcl2
from lark import Tree
from lark.exceptions import LarkError

BLOCKS = {"resource": ("resources", 2), "variable": ("variables", 1),
          "output": ("outputs", 1), "provider": ("providers", 1), "module": ("modules", 1)}


def _literal(node, source):
    """Only ordinary quoted literals; templates/heredocs are deliberately unknown."""
    if not isinstance(node, Tree):
        return None
    if node.data == "expr_term" and len(node.children) == 1:
        return _literal(node.children[0], source)
    if node.data != "string":
        return None
    raw = source[node.meta.start_pos:node.meta.end_pos]
    if "${" in raw or "%{" in raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, str) else None
    except ValueError:
        return None


def extract_terraform_structure(files):
    summary = {category: [] for category, _ in BLOCKS.values()}
    summary["files"] = []
    for path, source in sorted(files.items()):
        file_summary = {"file": path, "parse_status": "complete", "unparsed": []}
        summary["files"].append(file_summary)
        try:
            # Normalize Windows line endings only in the parser copy. Original
            # evidence stays untouched, and line numbers are unchanged.
            parser_source = source.replace("\r\n", "\n")
            tree = hcl2.parses(parser_source)
        except (LarkError, ValueError, TypeError, RecursionError) as error:
            file_summary.update(parse_status="failed", reason="invalid_or_unsupported_hcl")
            if isinstance(getattr(error, "line", None), int):
                file_summary["error_line"] = error.line
            continue
        for block in tree.children[0].children:
            if not isinstance(block, Tree) or block.data == "new_line_or_comment":
                continue
            location = {"file": path, "start_line": block.meta.line, "end_line": block.meta.end_line}
            kind = str(block.children[0].children[0]) if block.data == "block" else None
            labels = [_literal(label, parser_source) for label in block.children[1:-1]] if kind else []
            if kind not in BLOCKS or len(labels) != BLOCKS[kind][1] or any(label is None for label in labels):
                file_summary["parse_status"] = "partial"
                file_summary["unparsed"].append({**location, "reason": "unsupported_block_or_labels"})
                continue
            entry = {**location, "name": labels[-1]}
            if kind == "resource":
                entry["type"] = labels[0]
            if kind == "module":
                attributes = [child for child in block.children[-1].children
                              if isinstance(child, Tree) and child.data == "attribute"
                              and str(child.children[0].children[0]) == "source"]
                value = _literal(attributes[0].children[-1], parser_source) if len(attributes) == 1 else None
                entry.update(source=value, source_status="static" if value is not None else "unknown")
                if value is None:
                    file_summary["parse_status"] = "partial"
                    file_summary["unparsed"].append({**location, "reason": "module_source_not_static_or_missing"})
            summary[BLOCKS[kind][0]].append(entry)
    statuses = [item["parse_status"] for item in summary["files"]]
    summary["parse_status"] = ("complete" if all(status == "complete" for status in statuses) else
                               "failed" if all(status == "failed" for status in statuses) else "partial")
    return summary
