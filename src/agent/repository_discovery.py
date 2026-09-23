"""Request-local repository evidence; never an authorization policy."""

import json


class RepositoryDiscovery:
    def __init__(self):
        self.pages = {}
        self.allowed_ids = set()

    def record(self, arguments, content, *, truncated=False):
        """Accept only complete JSON listings, never IDs extracted from prose."""
        self.allowed_ids.clear()
        name = arguments.get("repoNameFilter", "")
        top, skip = arguments.get("top", 100), arguments.get("skip", 0)
        invalid = {"status": "unusable", "message": "Repository discovery is incomplete or unusable; retry a smaller page."}
        try:
            if truncated:
                raise ValueError
            if isinstance(content, str):
                rows = json.loads(content)
            else:
                rows = json.loads("\n".join(block["text"] for block in content
                                           if block.get("type") == "text"))
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
                return {"status": "selected", "name": name, "repository_id": next(iter(matches))}
            status = "ambiguous" if len(matches) > 1 or len(repositories) > 1 else "not_found"
            return {"status": status, "message": "No unique exact repository match. Specify an exact repository name."}
        if any(len(ids) != 1 for ids in repositories.values()):
            return {"status": "ambiguous", "message": "Repository names have conflicting IDs; do not select a repository."}
        self.allowed_ids = {repository_id for ids in repositories.values() for repository_id in ids}
        return {"status": "listed" if repositories else "not_found",
                "message": "Use only listed IDs and exact names; clarify ambiguous names before file access."}
