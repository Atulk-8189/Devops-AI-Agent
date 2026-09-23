"""Conservative lexical relationships among collected Terraform blocks only."""

import posixpath

import hcl2
from lark import Tree
from lark.exceptions import LarkError

from src.review.terraform_structure import BLOCKS, _literal, extract_terraform_structure


def extract_terraform_relationships(files):
    """Return lexical edges, scoped to a directory, without loading any files.

    Complete means complete for this deliberately limited syntax subset, not a
    complete Terraform dependency graph or proof that discovery was exhaustive.
    """
    structure = extract_terraform_structure(files)
    relationships, unresolved, symbols = [], [], {}
    locations = {}
    for kind, (category, _) in BLOCKS.items():
        for block in structure[category]:
            address = (block["type"] + "." + block["name"] if kind == "resource" else
                       ("var" if kind == "variable" else kind) + "." + block["name"])
            scope = posixpath.dirname(block["file"])
            symbols.setdefault((scope, address), []).append(block)
            locations[(block["file"], block["start_line"], kind, block["name"])] = address
    for info in structure["files"]:
        for issue in info["unparsed"]:
            unresolved.append({"source_file": info["file"], "line": issue["start_line"], "reason": issue["reason"]})
        if info["parse_status"] == "failed":
            unresolved.append({"source_file": info["file"], "line": info.get("error_line"), "reason": "unparsed_file"})

    def chain(node):
        if not isinstance(node, Tree):
            return None
        if node.data == "expr_term" and len(node.children) == 1:
            return chain(node.children[0])
        if node.data == "identifier":
            return [str(node.children[0])]
        if node.data == "get_attr_expr_term":
            base = chain(node.children[0])
            return base + [str(node.children[1].children[0].children[0])] if base else None
        return None

    def references(node, context):
        if not isinstance(node, Tree):
            return
        # Do not descend into constructs whose lexical scope or selected target
        # would require reasoning about expressions/iteration/templates.
        if any(str(t.data) in {"index_expr_term", "attr_splat_expr_term", "full_splat_expr_term",
                               "for_tuple_expr", "for_object_expr", "conditional", "function_call",
                               "interpolation", "heredoc_template", "heredoc_template_trim", "object"}
               for t in node.iter_subtrees()):
            unresolved.append({**context, "line": node.meta.line, "reason": "dynamic_or_unsupported_expression"})
            return
        if node.data == "get_attr_expr_term":
            parts = chain(node)
            if parts and len(parts) >= 2:
                target = ".".join(parts[:2])
                candidates = symbols.get((context["module_directory"], target), [])
                if parts[0] in {"local", "data", "path", "terraform", "count", "each", "self", "provider", "output"}:
                    candidates = []
                if len(candidates) == 1:
                    relationships.append({**context, "line": node.meta.line,
                                          "relationship": "references", "to": target})
                else:
                    unresolved.append({**context, "line": node.meta.line, "reference": ".".join(parts),
                                       "reason": "ambiguous_target" if candidates else "target_not_collected_or_unsupported"})
            else:
                unresolved.append({**context, "line": node.meta.line, "reason": "unsupported_reference"})
            return
        if node.data == "identifier" and str(node.children[0]) not in {"true", "false", "null"}:
            unresolved.append({**context, "line": node.meta.line, "reason": "unsupported_bare_reference"})
            return
        for child in node.children:
            references(child, context)

    collected_directories = {posixpath.dirname(path) for path in files}
    for path, source in sorted(files.items()):
        normalized = source.replace("\r\n", "\n")
        try:
            tree = hcl2.parses(normalized)
        except (LarkError, ValueError, TypeError, RecursionError):
            continue
        for block in tree.children[0].children:
            if not isinstance(block, Tree) or block.data != "block":
                continue
            kind = str(block.children[0].children[0])
            labels = [_literal(label, normalized) for label in block.children[1:-1]]
            if not labels:
                continue
            owner = locations.get((path, block.meta.line, kind, labels[-1]))
            if owner is None:
                continue
            scope = posixpath.dirname(path)
            context = {"from": owner, "source_file": path, "module_directory": scope}
            if len(symbols[(scope, owner)]) != 1:
                unresolved.append({**context, "line": block.meta.line, "reason": "ambiguous_owner"})
                continue

            def visit_body(body):
                for child in body.children:
                    if not isinstance(child, Tree):
                        continue
                    if child.data == "attribute":
                        if kind == "module" and body is block.children[-1] and str(child.children[0].children[0]) == "source":
                            declaration = symbols[(scope, owner)][0]
                            value = declaration["source"] if declaration.get("source_status") == "static" else None
                            target = posixpath.normpath(posixpath.join(scope, value)) if value and value.startswith(("./", "../")) else None
                            if target and (target == "/terraform" or target.startswith("/terraform/")) and target in collected_directories:
                                relationships.append({**context, "line": child.meta.line, "relationship": "local_source", "to": target})
                            else:
                                unresolved.append({**context, "line": child.meta.line,
                                                   "reason": "local_source_not_collected" if target else "external_or_dynamic_module_source"})
                        else:
                            references(child.children[-1], context)
                    elif child.data == "block":
                        if str(child.children[0].children[0]) == "dynamic":
                            unresolved.append({**context, "line": child.meta.line, "reason": "dynamic_block"})
                        else:
                            visit_body(child.children[-1])

            visit_body(block.children[-1])
    def ordered_unique(items):
        unique = {tuple(sorted(item.items())): item for item in items}
        return sorted(unique.values(), key=lambda item: (item["source_file"], item.get("line") or 0, str(sorted(item.items()))))
    return {"relationships": ordered_unique(relationships), "unresolved_references": ordered_unique(unresolved),
            "relationship_status": "partial" if unresolved else "complete"}
