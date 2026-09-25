"""Request-local repository evidence; never an authorization policy."""

import json
import re


class RepositoryDiscovery:
    def __init__(self):
        self.pages = {}
        self.allowed_ids = set()
        self.selected_name = None
        self.selected_id = None

    def clear_verified(self):
        self.allowed_ids.clear()
        self.selected_name = self.selected_id = None

    def file_repository_id(self, reference):
        """Resolve only an exact selected name; never guess or replace another ID."""
        if (isinstance(reference, str) and reference == self.selected_name
                and self.selected_id in self.allowed_ids):
            return self.selected_id
        return reference

    def record(self, arguments, content, *, truncated=False):
        """Accept only complete JSON listings, never IDs extracted from prose."""
        self.clear_verified()
        name = arguments.get("repoNameFilter", "")
        top, skip = arguments.get("top", 100), arguments.get("skip", 0)
        invalid = {"status": "unusable", "message": "Repository discovery is incomplete or unusable; retry a smaller page."}
        try:
            if truncated:
                raise ValueError
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            texts = []
            for block in blocks:
                if block.get("type") == "text":
                    text = block["text"]
                    # MCP may delimit untrusted JSON; unwrap, never interpret it.
                    match = re.fullmatch(r'<<([^>]+)>>[^\n]*\n([\s\S]*)\n<</\1>>', text)
                    texts.append(match.group(2) if match else text)
            rows = json.loads("\n".join(texts))
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"] or not isinstance(row.get("name"), str) or not row["name"]
                for row in rows
            ):
                raise ValueError
            if (isinstance(top, bool) or isinstance(skip, bool) or top <= 0 or skip < 0
                    or int(top) != top or int(skip) != skip or len(rows) > top):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            self.pages.pop(name, None)
            return invalid

        pages = self.pages.setdefault(name, {})
        if skip == 0:
            pages.clear()
        pages[skip] = (top, rows)
        offset, repositories = 0, {}
        while offset in pages:
            page_size, page = pages[offset]
            for row in page:
                repositories.setdefault(row["name"], set()).add(row["id"])
            if len(page) < page_size:
                break
            offset += page_size
        else:
            return {"status": "incomplete", "next_skip": offset,
                    "message": "More repository pages are needed before selection."}

        matches = repositories.get(name, set()) if name else set()
        if name:
            if len(matches) == 1:
                self.allowed_ids = matches
                self.selected_name, self.selected_id = name, next(iter(matches))
                return {"status": "selected", "name": name, "repository_id": next(iter(matches))}
            status = "ambiguous" if len(matches) > 1 or len(repositories) > 1 else "not_found"
            return {"status": status, "message": "No unique exact repository match. Specify an exact repository name."}
        if any(len(ids) != 1 for ids in repositories.values()):
            return {"status": "ambiguous", "message": "Repository names have conflicting IDs; do not select a repository."}
        self.allowed_ids = {repository_id for ids in repositories.values() for repository_id in ids}
        return {"status": "listed" if repositories else "not_found",
                "message": "Use only listed IDs and exact names; clarify ambiguous names before file access."}
