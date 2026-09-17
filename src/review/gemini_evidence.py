"""Deterministic, policy-checked MCP evidence collection for Gemini reviews."""
import json
import posixpath
import re
from uuid import uuid4

from langchain_core.messages import AIMessage, ToolMessage
from src.policy.policy import ALLOWED_PROJECT, enforce_read_only_policy
from src.review.review_validation import retrieved_files


def decode_listing(content):
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    texts = []
    for block in blocks:
        if block.get("type") == "text":
            text = block["text"]
            match = re.fullmatch(r'<<([^>]+)>>[^\n]*\n([\s\S]*)\n<</\1>>', text)
            texts.append(match.group(2) if match else text)
    return json.loads("\n".join(texts))


async def gather_terraform_evidence(tools, log=print):
    by_name = {tool.name: tool for tool in tools}
    messages = []

    async def invoke(name, **args):
        call = {"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}
        enforce_read_only_policy(call)
        log(f"Policy result: allowed; MCP tool: {name}; arguments: {json.dumps(args)}")
        # Pass the normalized call itself, retaining ToolMessage provenance.
        result = await by_name[name].ainvoke(call)
        if not isinstance(result, ToolMessage):
            raise TypeError("MCP tool did not return a ToolMessage")
        if result.status == "error":
            raise RuntimeError(f"MCP tool failed: {name}")
        messages.extend([AIMessage(content="", tool_calls=[call]), result])
        return result.content

    repositories = decode_listing(await invoke("repo_repository", action="list", project=ALLOWED_PROJECT))
    matches = [repo for repo in repositories if repo.get("name") == ALLOWED_PROJECT]
    if len(matches) != 1 or not matches[0].get("id"):
        raise ValueError("Expected exactly one repository named " + ALLOWED_PROJECT)
    repository_id = matches[0]["id"]
    listing = decode_listing(await invoke(
        "repo_file", action="list_directory", project=ALLOWED_PROJECT,
        repositoryId=repository_id, path="/terraform", recursive=True,
    ))
    paths = set()
    for item in listing["items"]:
        path = posixpath.normpath("/" + item["path"].lstrip("/"))
        if (path.startswith("/terraform/") and path.endswith(".tf")
                and not item.get("isFolder") and item.get("gitObjectType") != 2
                and ".terraform" not in path.split("/")):
            paths.add(path)
    if not paths:
        raise ValueError("No Terraform source files found under /terraform")
    for path in sorted(paths):
        await invoke("repo_file", action="get_content", project=ALLOWED_PROJECT,
                     repositoryId=repository_id, path=path)
    if set(retrieved_files(messages)) != paths:
        raise ValueError("Not all selected Terraform files returned usable evidence")
    return messages
