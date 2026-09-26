"""Deterministic, policy-checked MCP evidence collection for Terraform reviews."""
import json
import logging
import posixpath
import re
from uuid import uuid4
from src.safe_diagnostics import diagnostic_line, classify_error, error_content_text

from langchain_core.messages import AIMessage, ToolMessage
from src.policy.policy import ALLOWED_PROJECT, enforce_read_only_policy
from src.review.review_validation import retrieved_files

TERRAFORM_ROOT = "/terraform"
MAX_TERRAFORM_DISCOVERY_DEPTH = 3  # File depth relative to /terraform; root files are depth 1.
MAX_TERRAFORM_DIRECTORY_LISTINGS = 20
REPOSITORY_PAGE_SIZE = 100
MAX_REPOSITORY_DISCOVERY_PAGES = 5


class TerraformEvidence(list):
    """Retain ToolMessage provenance and list compatibility, plus discovery coverage."""

    def __init__(self):
        super().__init__()
        self.discovery = {
            "repository_name": ALLOWED_PROJECT, "branch": "main", "terraform_root": TERRAFORM_ROOT,
            "selected_files": [], "listed_directories": [], "skipped_directories": [],
            "incomplete": False, "limit_reasons": [],
            "max_depth": MAX_TERRAFORM_DISCOVERY_DEPTH,
            "max_directory_listings": MAX_TERRAFORM_DIRECTORY_LISTINGS,
        }


def decode_listing(content):
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    texts = []
    if not isinstance(blocks, list):
        raise ValueError("Invalid MCP listing response")
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            text = block["text"]
            match = re.fullmatch(r'<<([^>]+)>>[^\n]*\n([\s\S]*)\n<</\1>>', text)
            texts.append(match.group(2) if match else text)
    try:
        return json.loads("\n".join(texts))
    except (ValueError, RecursionError) as error:
        raise ValueError("Invalid MCP listing JSON") from error


def _diagnostic_log(message: str) -> None:
    logging.getLogger("src.diagnostics").info(message)


async def gather_terraform_evidence(tools, log=_diagnostic_log):
    by_name = {tool.name: tool for tool in tools}
    messages = TerraformEvidence()

    async def invoke(name, **args):
        call = {"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}
        enforce_read_only_policy(call)
        log(diagnostic_line(event="collection_allowed", tool_name=name, operation=args.get("action"), outcome="allowed"))
        # Pass the normalized call itself, retaining ToolMessage provenance.
        from src.observability import mcp_call
        result = await mcp_call(call, lambda: by_name[name].ainvoke(call))
        if not isinstance(result, ToolMessage):
            raise TypeError("MCP tool did not return a ToolMessage")
        if result.status == "error":
            safe = classify_error(RuntimeError(error_content_text(result.content)), boundary="tool")
            if safe.category in {"authentication", "authorization"}:
                raise PermissionError(safe.user_message())
            raise RuntimeError("MCP tool failed: " + safe.category + "; " + safe.reason_code)
        messages.extend([AIMessage(content="", tool_calls=[call]), result])
        return result.content

    matches = set()
    for page in range(MAX_REPOSITORY_DISCOVERY_PAGES):
        repositories = decode_listing(await invoke(
            "repo_repository", action="list", project=ALLOWED_PROJECT,
            top=REPOSITORY_PAGE_SIZE, skip=page * REPOSITORY_PAGE_SIZE,
        ))
        if not isinstance(repositories, list) or len(repositories) > REPOSITORY_PAGE_SIZE or any(
            not isinstance(repo, dict) or not isinstance(repo.get("name"), str)
            or not isinstance(repo.get("id"), str) or not repo["id"] for repo in repositories
        ):
            raise ValueError("Invalid repository discovery response")
        matches.update(repo["id"] for repo in repositories if repo["name"] == ALLOWED_PROJECT)
        if len(matches) > 1:
            raise ValueError("Ambiguous repository name: " + ALLOWED_PROJECT)
        if len(repositories) < REPOSITORY_PAGE_SIZE:
            break
    else:
        raise ValueError("Repository discovery incomplete: page limit reached; no repository selected")
    if len(matches) != 1:
        raise ValueError("Expected exactly one repository named " + ALLOWED_PROJECT)
    repository_id = next(iter(matches))
    messages.discovery["repository_id"] = repository_id
    messages.discovery["repository_pages"] = page + 1
    paths = set()
    pending, visited, skipped = [(TERRAFORM_ROOT, 0)], set(), set()
    while pending:
        if len(visited) >= MAX_TERRAFORM_DIRECTORY_LISTINGS:
            skipped.update(path for path, _ in pending)
            messages.discovery["limit_reasons"].append("directory_listing_limit")
            break
        directory, depth = pending.pop(0)
        if directory in visited:
            continue
        visited.add(directory)
        listing = decode_listing(await invoke(
            "repo_file", action="list_directory", project=ALLOWED_PROJECT,
            repositoryId=repository_id, path=directory, recursive=False, recursionDepth=1,
        ))
        if not isinstance(listing, dict) or not isinstance(listing.get("items"), list):
            raise ValueError("Invalid Terraform directory listing")
        children = set()
        for item in listing["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("Invalid Terraform directory entry")
            commit_id = item.get("commitId")
            if isinstance(commit_id, str) and "commit_sha" not in messages.discovery:
                messages.discovery["commit_sha"] = commit_id
                messages.discovery["commit_id"] = commit_id
            if "isFolder" in item and type(item["isFolder"]) is not bool:
                raise ValueError("Invalid Terraform directory entry type")
            path = posixpath.normpath("/" + item["path"].lstrip("/"))
            if not path.startswith(TERRAFORM_ROOT + "/") or ".terraform" in path.split("/"):
                continue
            if path == directory:
                continue
            if posixpath.dirname(path) != directory:
                raise ValueError("Unexpected entry outside the requested one-level directory")
            folder = item.get("isFolder") is True or item.get("gitObjectType") in (2, "tree")
            if folder:
                if depth + 1 >= MAX_TERRAFORM_DISCOVERY_DEPTH:
                    skipped.add(path)
                    messages.discovery["limit_reasons"].append("depth_limit")
                else:
                    children.add(path)
            elif path.endswith(".tf"):
                paths.add(path)
        pending.extend((path, depth + 1) for path in sorted(children) if path not in visited)
    messages.discovery.update(selected_files=sorted(paths), listed_directories=sorted(visited),
                              skipped_directories=sorted(skipped), incomplete=bool(skipped),
                              limit_reasons=sorted(set(messages.discovery["limit_reasons"])))
    if not paths:
        raise ValueError("No Terraform source files found under /terraform" +
                         ("; discovery incomplete because of limits" if skipped else ""))
    for path in sorted(paths):
        await invoke("repo_file", action="get_content", project=ALLOWED_PROJECT,
                     repositoryId=repository_id, path=path)
    if set(retrieved_files(messages)) != paths:
        raise ValueError("Not all selected Terraform files returned usable evidence")
    return messages
