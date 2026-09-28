"""Local SQLite Data Layer for Chainlit chat history and thread persistence."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

try:
    from chainlit.data.base import BaseDataLayer
    from chainlit.types import (
        Feedback,
        PageInfo,
        PaginatedResponse,
        Pagination,
        ThreadDict,
        ThreadFilter,
    )
    from chainlit.element import Element, ElementDict
    from chainlit.step import StepDict
    from chainlit.user import PersistedUser, User
    CHAINLIT_INSTALLED = True
except ImportError:
    CHAINLIT_INSTALLED = False

    class BaseDataLayer:  # type: ignore[no-redef]
        """Fallback BaseDataLayer when Chainlit is not installed."""
        pass

    class PageInfo:  # type: ignore[no-redef]
        def __init__(self, hasNextPage: bool = False, startCursor: Optional[str] = None, endCursor: Optional[str] = None):
            self.hasNextPage = hasNextPage
            self.startCursor = startCursor
            self.endCursor = endCursor

        def to_dict(self):
            return {
                "hasNextPage": self.hasNextPage,
                "startCursor": self.startCursor,
                "endCursor": self.endCursor,
            }

    class PaginatedResponse:  # type: ignore[no-redef]
        def __init__(self, pageInfo: PageInfo, data: List[Any]):
            self.pageInfo = pageInfo
            self.data = data

        def to_dict(self):
            return {
                "pageInfo": self.pageInfo.to_dict(),
                "data": [
                    (d.to_dict() if hasattr(d, "to_dict") and callable(d.to_dict) else d)
                    for d in self.data
                ],
            }

    class Pagination:  # type: ignore[no-redef]
        def __init__(self, first: int = 20, cursor: Optional[str] = None):
            self.first = first
            self.cursor = cursor

    class ThreadFilter:  # type: ignore[no-redef]
        def __init__(self, feedback: Optional[int] = None, userId: Optional[str] = None, search: Optional[str] = None):
            self.feedback = feedback
            self.userId = userId
            self.search = search

    class User:  # type: ignore[no-redef]
        def __init__(self, identifier: str, metadata: Optional[Dict] = None, id: Optional[str] = None):
            self.identifier = identifier
            self.metadata = metadata or {}
            self.id = id or identifier

    class PersistedUser(User):  # type: ignore[no-redef]
        def __init__(self, identifier: str, metadata: Optional[Dict] = None, id: Optional[str] = None, createdAt: Optional[str] = None):
            super().__init__(identifier=identifier, metadata=metadata, id=id)
            self.createdAt = createdAt

    ThreadDict = Dict[str, Any]  # type: ignore[misc]
    StepDict = Dict[str, Any]  # type: ignore[misc]
    ElementDict = Dict[str, Any]  # type: ignore[misc]
    Feedback = Any  # type: ignore[misc]
    Element = Any  # type: ignore[misc]


logger = logging.getLogger(__name__)


def clean_thread_title(text: str, max_length: int = 55) -> str:
    """Generate a clean, meaningful conversation title from the first message without altering content."""
    if not text or not text.strip():
        return "DevOps Investigation"
    first_line = text.strip().splitlines()[0].strip()
    # Strip markdown headers, bold, italics, backticks, bullet prefixes
    cleaned = re.sub(r"^[#\*\`\>\-\+\s]+", "", first_line)
    cleaned = re.sub(r"[\`\*\_]+", "", cleaned).strip()
    if not cleaned:
        return "DevOps Investigation"
    if len(cleaned) <= max_length:
        return cleaned
    truncated = cleaned[:max_length].rsplit(" ", 1)[0]
    return f"{truncated}…" if truncated else f"{cleaned[:max_length]}…"


class SQLiteDataLayer(BaseDataLayer):
    """Local, file-backed SQLite persistence layer for Chainlit conversation history."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path is None:
            env_path = os.environ.get("CHAINLIT_DB_PATH")
            if env_path:
                self.db_path = str(env_path)
            else:
                files_dir = Path(__file__).resolve().parent / ".files"
                files_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(files_dir / "chat_history.db")
        else:
            self.db_path = str(db_path)

        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON;")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                identifier TEXT UNIQUE NOT NULL,
                metadata TEXT,
                createdAt TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                createdAt TEXT NOT NULL,
                name TEXT,
                userId TEXT,
                userIdentifier TEXT,
                tags TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS steps (
                id TEXT PRIMARY KEY,
                threadId TEXT NOT NULL,
                parentId TEXT,
                name TEXT,
                type TEXT,
                command TEXT,
                modes TEXT,
                streaming INTEGER DEFAULT 0,
                waitForAnswer INTEGER,
                isError INTEGER DEFAULT 0,
                metadata TEXT,
                tags TEXT,
                input TEXT,
                output TEXT,
                createdAt TEXT,
                start TEXT,
                end TEXT,
                generation TEXT,
                showInput TEXT,
                defaultOpen INTEGER,
                autoCollapse INTEGER,
                language TEXT,
                icon TEXT,
                feedback TEXT,
                step_order INTEGER,
                FOREIGN KEY(threadId) REFERENCES threads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS elements (
                id TEXT PRIMARY KEY,
                threadId TEXT NOT NULL,
                forId TEXT,
                name TEXT,
                type TEXT,
                url TEXT,
                chainlitKey TEXT,
                display TEXT,
                size TEXT,
                language TEXT,
                autoPlay INTEGER,
                playerConfig TEXT,
                props TEXT,
                mime TEXT,
                objectKey TEXT,
                content TEXT,
                FOREIGN KEY(threadId) REFERENCES threads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS feedbacks (
                id TEXT PRIMARY KEY,
                stepId TEXT,
                forId TEXT,
                value INTEGER,
                comment TEXT
            );
            """)

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT id, identifier, metadata, createdAt FROM users WHERE identifier = ?",
                (identifier,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            return PersistedUser(
                id=row["id"],
                identifier=row["identifier"],
                metadata=meta,
                createdAt=row["createdAt"]
            )

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        now = datetime.now(timezone.utc).isoformat()
        user_id = getattr(user, "id", None) or getattr(user, "identifier", "local-user")
        meta_str = json.dumps(getattr(user, "metadata", {}) or {})
        async with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO users (id, identifier, metadata, createdAt)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(identifier) DO UPDATE SET
                        metadata = excluded.metadata
                    """,
                    (user_id, user.identifier, meta_str, now)
                )
                cursor = self._conn.execute(
                    "SELECT id, identifier, metadata, createdAt FROM users WHERE identifier = ?",
                    (user.identifier,)
                )
                row = cursor.fetchone()
                return PersistedUser(
                    id=row["id"],
                    identifier=row["identifier"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                    createdAt=row["createdAt"]
                )

    async def get_thread_author(self, thread_id: str) -> str:
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT userIdentifier FROM threads WHERE id = ?",
                (thread_id,)
            )
            row = cursor.fetchone()
            return row["userIdentifier"] if row and row["userIdentifier"] else ""

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT id, createdAt, name, userId, userIdentifier, tags, metadata FROM threads WHERE id = ?",
                (thread_id,)
            )
            t_row = cursor.fetchone()
            if not t_row:
                return None

            steps_cursor = self._conn.execute(
                """
                SELECT id, threadId, parentId, name, type, command, modes, streaming,
                       waitForAnswer, isError, metadata, tags, input, output, createdAt,
                       start, end, generation, showInput, defaultOpen, autoCollapse,
                       language, icon, feedback, step_order
                FROM steps
                WHERE threadId = ?
                ORDER BY step_order ASC, createdAt ASC
                """,
                (thread_id,)
            )
            steps: List[StepDict] = []
            for s_row in steps_cursor.fetchall():
                steps.append({
                    "id": s_row["id"],
                    "threadId": s_row["threadId"],
                    "parentId": s_row["parentId"],
                    "name": s_row["name"] or "",
                    "type": s_row["type"] or "run",
                    "command": s_row["command"],
                    "modes": json.loads(s_row["modes"]) if s_row["modes"] else None,
                    "streaming": bool(s_row["streaming"]),
                    "waitForAnswer": bool(s_row["waitForAnswer"]) if s_row["waitForAnswer"] is not None else None,
                    "isError": bool(s_row["isError"]) if s_row["isError"] is not None else None,
                    "metadata": json.loads(s_row["metadata"]) if s_row["metadata"] else {},
                    "tags": json.loads(s_row["tags"]) if s_row["tags"] else None,
                    "input": s_row["input"] or "",
                    "output": s_row["output"] or "",
                    "createdAt": s_row["createdAt"],
                    "start": s_row["start"],
                    "end": s_row["end"],
                    "generation": json.loads(s_row["generation"]) if s_row["generation"] else None,
                    "showInput": s_row["showInput"],
                    "defaultOpen": bool(s_row["defaultOpen"]) if s_row["defaultOpen"] is not None else None,
                    "autoCollapse": bool(s_row["autoCollapse"]) if s_row["autoCollapse"] is not None else None,
                    "language": s_row["language"],
                    "icon": s_row["icon"],
                    "feedback": json.loads(s_row["feedback"]) if s_row["feedback"] else None,
                })

            elem_cursor = self._conn.execute(
                """
                SELECT id, threadId, forId, name, type, url, chainlitKey, display,
                       size, language, autoPlay, playerConfig, props, mime, objectKey, content
                FROM elements
                WHERE threadId = ?
                """,
                (thread_id,)
            )
            elements: List[ElementDict] = []
            for e_row in elem_cursor.fetchall():
                elements.append({
                    "id": e_row["id"],
                    "threadId": e_row["threadId"],
                    "forId": e_row["forId"],
                    "name": e_row["name"] or "",
                    "type": e_row["type"] or "text",
                    "url": e_row["url"],
                    "chainlitKey": e_row["chainlitKey"],
                    "display": e_row["display"] or "side",
                    "size": e_row["size"],
                    "language": e_row["language"],
                    "autoPlay": bool(e_row["autoPlay"]) if e_row["autoPlay"] is not None else None,
                    "props": json.loads(e_row["props"]) if e_row["props"] else None,
                    "mime": e_row["mime"],
                    "objectKey": e_row["objectKey"],
                    "content": e_row["content"],
                })

            tags = json.loads(t_row["tags"]) if t_row["tags"] else []
            metadata = json.loads(t_row["metadata"]) if t_row["metadata"] else {}

            return {
                "id": t_row["id"],
                "createdAt": t_row["createdAt"],
                "name": t_row["name"] or "DevOps Investigation",
                "userId": t_row["userId"],
                "userIdentifier": t_row["userIdentifier"] or "local-user",
                "tags": tags,
                "metadata": metadata,
                "steps": steps,
                "elements": elements,
            }

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        clean_name = clean_thread_title(name) if name is not None else None
        async with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT id, name, userId, userIdentifier, tags, metadata FROM threads WHERE id = ?",
                    (thread_id,)
                )
                row = cursor.fetchone()
                if not row:
                    # New thread insertion
                    u_id = user_id or "local-user"
                    u_ident = "local-user"
                    if user_id:
                        u_cur = self._conn.execute("SELECT identifier FROM users WHERE id = ?", (user_id,))
                        u_row = u_cur.fetchone()
                        if u_row:
                            u_ident = u_row["identifier"]
                    self._conn.execute(
                        """
                        INSERT INTO threads (id, createdAt, name, userId, userIdentifier, tags, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            thread_id,
                            now,
                            clean_name or "DevOps Investigation",
                            u_id,
                            u_ident,
                            json.dumps(tags or []),
                            json.dumps(metadata or {}),
                        )
                    )
                else:
                    new_name = clean_name if clean_name is not None else row["name"]
                    new_user_id = user_id if user_id is not None else row["userId"]
                    new_tags = json.dumps(tags) if tags is not None else row["tags"]
                    new_meta = json.dumps(metadata) if metadata is not None else row["metadata"]
                    self._conn.execute(
                        """
                        UPDATE threads
                        SET name = ?, userId = ?, tags = ?, metadata = ?
                        WHERE id = ?
                        """,
                        (new_name, new_user_id, new_tags, new_meta, thread_id)
                    )

    async def ensure_thread_title(self, thread_id: str, first_message: str, user_id: Optional[str] = None) -> None:
        """Set a clean title for a thread if it does not yet have a custom name."""
        clean_name = clean_thread_title(first_message)
        async with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT name FROM threads WHERE id = ?",
                    (thread_id,)
                )
                row = cursor.fetchone()
                if not row:
                    now = datetime.now(timezone.utc).isoformat()
                    self._conn.execute(
                        """
                        INSERT INTO threads (id, createdAt, name, userId, userIdentifier, tags, metadata)
                        VALUES (?, ?, ?, ?, ?, '[]', '{}')
                        """,
                        (thread_id, now, clean_name, user_id or "local-user", "local-user")
                    )
                elif not row["name"] or row["name"] == "DevOps Investigation":
                    self._conn.execute(
                        "UPDATE threads SET name = ? WHERE id = ?",
                        (clean_name, thread_id)
                    )

    async def delete_thread(self, thread_id: str) -> None:
        async with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse:
        search_term = filters.search.strip() if (filters and filters.search) else None
        user_id = filters.userId if (filters and filters.userId) else None

        async with self._lock:
            # Query all matching threads ordered by most recent activity
            query = """
            SELECT t.id, t.createdAt, t.name, t.userId, t.userIdentifier, t.tags, t.metadata,
                   COALESCE(MAX(s.createdAt), t.createdAt) AS updatedAt
            FROM threads t
            LEFT JOIN steps s ON t.id = s.threadId
            WHERE (t.userId = ? OR ? IS NULL)
            """
            params: List[Any] = [user_id, user_id]

            if search_term:
                query += " AND (t.name LIKE ? OR s.input LIKE ? OR s.output LIKE ?)"
                pattern = f"%{search_term}%"
                params.extend([pattern, pattern, pattern])

            query += " GROUP BY t.id ORDER BY updatedAt DESC"

            cursor = self._conn.execute(query, tuple(params))
            rows = cursor.fetchall()

            all_threads: List[ThreadDict] = []
            for r in rows:
                t_id = r["id"]
                # Fetch step summary or count for filtering/preview
                s_cursor = self._conn.execute(
                    "SELECT id, name, type, input, output, createdAt, feedback FROM steps WHERE threadId = ? ORDER BY step_order ASC",
                    (t_id,)
                )
                steps = []
                for sr in s_cursor.fetchall():
                    fb = json.loads(sr["feedback"]) if sr["feedback"] else None
                    steps.append({
                        "id": sr["id"],
                        "name": sr["name"],
                        "type": sr["type"],
                        "input": sr["input"] or "",
                        "output": sr["output"] or "",
                        "createdAt": sr["createdAt"],
                        "feedback": fb,
                    })

                all_threads.append({
                    "id": t_id,
                    "createdAt": r["createdAt"],
                    "name": r["name"] or "DevOps Investigation",
                    "userId": r["userId"],
                    "userIdentifier": r["userIdentifier"] or "local-user",
                    "tags": json.loads(r["tags"]) if r["tags"] else [],
                    "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                    "steps": steps,
                    "elements": [],
                })

        # Cursor pagination
        start_idx = 0
        if pagination and getattr(pagination, "cursor", None):
            for i, th in enumerate(all_threads):
                if th["id"] == pagination.cursor:
                    start_idx = i + 1
                    break

        first = pagination.first if (pagination and getattr(pagination, "first", None)) else 20
        end_idx = start_idx + first
        page_threads = all_threads[start_idx:end_idx]
        has_next_page = len(all_threads) > end_idx
        start_cursor = page_threads[0]["id"] if page_threads else None
        end_cursor = page_threads[-1]["id"] if page_threads else None

        page_info = PageInfo(
            hasNextPage=has_next_page,
            startCursor=start_cursor,
            endCursor=end_cursor,
        )
        return PaginatedResponse(pageInfo=page_info, data=page_threads)

    async def create_step(self, step_dict: StepDict) -> None:
        thread_id = step_dict.get("threadId")
        if not thread_id:
            return

        now = datetime.now(timezone.utc).isoformat()
        step_id = step_dict.get("id") or str(uuid.uuid4())

        async with self._lock:
            with self._conn:
                # Ensure thread row exists
                t_cur = self._conn.execute("SELECT id FROM threads WHERE id = ?", (thread_id,))
                if not t_cur.fetchone():
                    self._conn.execute(
                        """
                        INSERT INTO threads (id, createdAt, name, userId, userIdentifier, tags, metadata)
                        VALUES (?, ?, 'DevOps Investigation', 'local-user', 'local-user', '[]', '{}')
                        """,
                        (thread_id, step_dict.get("createdAt") or now)
                    )

                # Determine next sequence order
                order_cur = self._conn.execute(
                    "SELECT COALESCE(MAX(step_order), 0) + 1 AS next_order FROM steps WHERE threadId = ?",
                    (thread_id,)
                )
                next_order = order_cur.fetchone()["next_order"]

                modes_str = json.dumps(step_dict.get("modes")) if step_dict.get("modes") else None
                meta_str = json.dumps(step_dict.get("metadata") or {})
                tags_str = json.dumps(step_dict.get("tags")) if step_dict.get("tags") else None
                gen_str = json.dumps(step_dict.get("generation")) if step_dict.get("generation") else None
                fb_str = json.dumps(step_dict.get("feedback")) if step_dict.get("feedback") else None

                self._conn.execute(
                    """
                    INSERT INTO steps (
                        id, threadId, parentId, name, type, command, modes, streaming,
                        waitForAnswer, isError, metadata, tags, input, output, createdAt,
                        start, end, generation, showInput, defaultOpen, autoCollapse,
                        language, icon, feedback, step_order
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        output = excluded.output,
                        input = excluded.input,
                        metadata = excluded.metadata,
                        isError = excluded.isError,
                        end = excluded.end
                    """,
                    (
                        step_id,
                        thread_id,
                        step_dict.get("parentId"),
                        step_dict.get("name") or "",
                        step_dict.get("type") or "run",
                        step_dict.get("command"),
                        modes_str,
                        1 if step_dict.get("streaming") else 0,
                        1 if step_dict.get("waitForAnswer") else 0 if step_dict.get("waitForAnswer") is not None else None,
                        1 if step_dict.get("isError") else 0,
                        meta_str,
                        tags_str,
                        step_dict.get("input") or "",
                        step_dict.get("output") or "",
                        step_dict.get("createdAt") or now,
                        step_dict.get("start"),
                        step_dict.get("end"),
                        gen_str,
                        str(step_dict.get("showInput")) if step_dict.get("showInput") is not None else None,
                        1 if step_dict.get("defaultOpen") else 0 if step_dict.get("defaultOpen") is not None else None,
                        1 if step_dict.get("autoCollapse") else 0 if step_dict.get("autoCollapse") is not None else None,
                        step_dict.get("language"),
                        step_dict.get("icon"),
                        fb_str,
                        next_order,
                    )
                )

    async def update_step(self, step_dict: StepDict) -> None:
        step_id = step_dict.get("id")
        if not step_id:
            return
        async with self._lock:
            with self._conn:
                cur = self._conn.execute("SELECT id FROM steps WHERE id = ?", (step_id,))
                if not cur.fetchone():
                    # Fallback to create_step if step does not exist
                    pass
                else:
                    meta_str = json.dumps(step_dict.get("metadata") or {})
                    fb_str = json.dumps(step_dict.get("feedback")) if step_dict.get("feedback") else None
                    self._conn.execute(
                        """
                        UPDATE steps
                        SET output = ?, input = ?, metadata = ?, isError = ?, end = ?, feedback = ?
                        WHERE id = ?
                        """,
                        (
                            step_dict.get("output") or "",
                            step_dict.get("input") or "",
                            meta_str,
                            1 if step_dict.get("isError") else 0,
                            step_dict.get("end"),
                            fb_str,
                            step_id,
                        )
                    )

    async def delete_step(self, step_id: str) -> None:
        async with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM steps WHERE id = ?", (step_id,))

    async def create_element(self, element: Element) -> None:
        elem_id = getattr(element, "id", None) or (element.get("id") if isinstance(element, dict) else None)
        thread_id = getattr(element, "thread_id", None) or getattr(element, "threadId", None)
        if not thread_id and isinstance(element, dict):
            thread_id = element.get("threadId") or element.get("thread_id")
        if not elem_id or not thread_id:
            return

        for_id = getattr(element, "for_id", None) or getattr(element, "forId", None)
        if not for_id and isinstance(element, dict):
            for_id = element.get("forId") or element.get("for_id")

        name = getattr(element, "name", "") or (element.get("name", "") if isinstance(element, dict) else "")
        elem_type = getattr(element, "type", "text") or (element.get("type", "text") if isinstance(element, dict) else "text")
        url = getattr(element, "url", None) or (element.get("url") if isinstance(element, dict) else None)
        chainlit_key = getattr(element, "chainlit_key", None) or (element.get("chainlitKey") if isinstance(element, dict) else None)
        display = getattr(element, "display", "side") or (element.get("display", "side") if isinstance(element, dict) else "side")
        size = getattr(element, "size", None) or (element.get("size") if isinstance(element, dict) else None)
        language = getattr(element, "language", None) or (element.get("language") if isinstance(element, dict) else None)
        mime = getattr(element, "mime", None) or (element.get("mime") if isinstance(element, dict) else None)
        object_key = getattr(element, "object_key", None) or (element.get("objectKey") if isinstance(element, dict) else None)

        raw_content = getattr(element, "content", None) or (element.get("content") if isinstance(element, dict) else None)
        if isinstance(raw_content, bytes):
            content_str = raw_content.decode("utf-8", errors="replace")
        elif raw_content is not None:
            content_str = str(raw_content)
        else:
            content_str = None

        props = getattr(element, "props", None) or (element.get("props") if isinstance(element, dict) else None)
        props_str = json.dumps(props) if props else None

        async with self._lock:
            with self._conn:
                if name == "ExecutionTrace" and display == "side":
                    self._conn.execute(
                        "DELETE FROM elements WHERE threadId = ? AND name = 'ExecutionTrace'",
                        (thread_id,)
                    )
                self._conn.execute(
                    """
                    INSERT INTO elements (
                        id, threadId, forId, name, type, url, chainlitKey,
                        display, size, language, mime, objectKey, content, props
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        content = excluded.content,
                        url = excluded.url
                    """,
                    (
                        elem_id,
                        thread_id,
                        for_id,
                        name,
                        elem_type,
                        url,
                        chainlit_key,
                        display,
                        size,
                        language,
                        mime,
                        object_key,
                        content_str,
                        props_str,
                    )
                )

    async def get_element(self, thread_id: str, element_id: str) -> Optional[ElementDict]:
        async with self._lock:
            cursor = self._conn.execute(
                """
                SELECT id, threadId, forId, name, type, url, chainlitKey,
                       display, size, language, mime, objectKey, content, props
                FROM elements
                WHERE threadId = ? AND id = ?
                """,
                (thread_id, element_id)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "threadId": row["threadId"],
                "forId": row["forId"],
                "name": row["name"],
                "type": row["type"],
                "url": row["url"],
                "chainlitKey": row["chainlitKey"],
                "display": row["display"],
                "size": row["size"],
                "language": row["language"],
                "mime": row["mime"],
                "objectKey": row["objectKey"],
                "content": row["content"],
                "props": json.loads(row["props"]) if row["props"] else None,
            }

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None) -> None:
        async with self._lock:
            with self._conn:
                if thread_id:
                    self._conn.execute("DELETE FROM elements WHERE id = ? AND threadId = ?", (element_id, thread_id))
                else:
                    self._conn.execute("DELETE FROM elements WHERE id = ?", (element_id,))

    async def upsert_feedback(self, feedback: Feedback) -> str:
        fb_id = getattr(feedback, "id", None) or str(uuid.uuid4())
        step_id = getattr(feedback, "for_id", None) or getattr(feedback, "step_id", None)
        val = getattr(feedback, "value", 0)
        comment = getattr(feedback, "comment", None)
        async with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO feedbacks (id, stepId, forId, value, comment)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        value = excluded.value,
                        comment = excluded.comment
                    """,
                    (fb_id, step_id, step_id, val, comment)
                )
        return fb_id

    async def delete_feedback(self, feedback_id: str) -> bool:
        async with self._lock:
            with self._conn:
                cursor = self._conn.execute("DELETE FROM feedbacks WHERE id = ?", (feedback_id,))
                return cursor.rowcount > 0

    async def get_favorite_steps(self, user_id: str) -> List[StepDict]:
        return []

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()
