"""Unit tests for the SQLite conversation history and data layer."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from frontend.data_layer import (
    Pagination,
    SQLiteDataLayer,
    ThreadFilter,
    User,
    clean_thread_title,
)


class DataLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_chat_history.db"
        self.dl = SQLiteDataLayer(db_path=self.db_path)

    async def asyncTearDown(self):
        await self.dl.close()
        self.temp_dir.cleanup()

    def test_clean_thread_title(self):
        # Plain simple text
        self.assertEqual(clean_thread_title("Check AKS cluster health"), "Check AKS cluster health")

        # Markdown header prefix
        self.assertEqual(clean_thread_title("## AKS Pod Failure Investigation"), "AKS Pod Failure Investigation")
        self.assertEqual(clean_thread_title("# Main Deployment Issue"), "Main Deployment Issue")

        # Bullet and markdown formatting
        self.assertEqual(clean_thread_title("- **Investigate** memory pressure"), "Investigate memory pressure")
        self.assertEqual(clean_thread_title("`kubectl get pods` failed"), "kubectl get pods failed")

        # Multiline text - takes first non-empty line
        multiline = "Investigate Task Manager\nDetailed description of failure on node-1"
        self.assertEqual(clean_thread_title(multiline), "Investigate Task Manager")

        # Empty / whitespace fallback
        self.assertEqual(clean_thread_title("   "), "DevOps Investigation")
        self.assertEqual(clean_thread_title(""), "DevOps Investigation")
        self.assertEqual(clean_thread_title("### "), "DevOps Investigation")

        # Long text word-boundary truncation
        long_text = "Check if the application gateway ingress controller is properly synchronizing ingress resources"
        title = clean_thread_title(long_text, max_length=50)
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 52)
        self.assertTrue(title.startswith("Check if the application gateway ingress"))

    async def test_user_lifecycle(self):
        # Nonexistent user
        user = await self.dl.get_user("unknown-user")
        self.assertIsNone(user)

        # Create user
        new_user = User(identifier="local-user", metadata={"role": "admin"})
        created = await self.dl.create_user(new_user)
        self.assertIsNotNone(created)
        self.assertEqual(created.identifier, "local-user")
        self.assertEqual(created.metadata.get("role"), "admin")

        # Retrieve user
        retrieved = await self.dl.get_user("local-user")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, created.id)
        self.assertEqual(retrieved.identifier, "local-user")

    async def test_thread_creation_and_title_generation(self):
        thread_id = "thread-aks-001"
        first_msg = "## AKS Cluster Issue: ImagePullBackOff on task-manager"

        # Update thread with raw first message - title is automatically cleaned
        await self.dl.update_thread(thread_id=thread_id, name=first_msg, user_id="local-user")

        thread = await self.dl.get_thread(thread_id)
        self.assertIsNotNone(thread)
        self.assertEqual(thread["id"], thread_id)
        self.assertEqual(thread["name"], "AKS Cluster Issue: ImagePullBackOff on task-manager")
        self.assertEqual(thread["userIdentifier"], "local-user")

        # Author verification
        author = await self.dl.get_thread_author(thread_id)
        self.assertEqual(author, "local-user")

    async def test_conversation_persistence_and_retrieval(self):
        thread_id = "thread-persist-002"
        await self.dl.update_thread(thread_id=thread_id, name="Check Terraform Drift", user_id="local-user")

        # Add user message step
        user_step = {
            "id": "step-user-1",
            "threadId": thread_id,
            "name": "user",
            "type": "user_message",
            "input": "Check Terraform state drift for vnet",
            "output": "Check Terraform state drift for vnet",
            "createdAt": "2026-09-27T10:00:00Z",
        }
        await self.dl.create_step(user_step)

        # Add agent message step with formatted output
        agent_step = {
            "id": "step-agent-1",
            "threadId": thread_id,
            "name": "agent",
            "type": "assistant_message",
            "input": "",
            "output": "### 📋 Summary\nNo drift detected across subnets.",
            "createdAt": "2026-09-27T10:00:05Z",
        }
        await self.dl.create_step(agent_step)

        # Attach side element (Raw response)
        raw_element = {
            "id": "elem-raw-1",
            "threadId": thread_id,
            "forId": "step-agent-1",
            "name": "Raw response",
            "type": "text",
            "display": "side",
            "content": '{"drift": false, "resources": 4}',
        }
        await self.dl.create_element(raw_element)

        # Fetch thread and verify contents
        thread = await self.dl.get_thread(thread_id)
        self.assertIsNotNone(thread)
        self.assertEqual(len(thread["steps"]), 2)
        self.assertEqual(thread["steps"][0]["id"], "step-user-1")
        self.assertEqual(thread["steps"][0]["output"], "Check Terraform state drift for vnet")
        self.assertEqual(thread["steps"][1]["id"], "step-agent-1")
        self.assertIn("### 📋 Summary", thread["steps"][1]["output"])

        self.assertEqual(len(thread["elements"]), 1)
        self.assertEqual(thread["elements"][0]["name"], "Raw response")
        self.assertEqual(thread["elements"][0]["content"], '{"drift": false, "resources": 4}')

        # Fetch element directly
        elem = await self.dl.get_element(thread_id, "elem-raw-1")
        self.assertIsNotNone(elem)
        self.assertEqual(elem["content"], '{"drift": false, "resources": 4}')

    async def test_restart_survival(self):
        """Verify chat history survives frontend / datalayer restart using the SQLite file."""
        thread_id = "thread-restart-003"
        await self.dl.update_thread(thread_id=thread_id, name="Restart Survival Test", user_id="local-user")
        await self.dl.create_step({
            "id": "step-restart-1",
            "threadId": thread_id,
            "name": "user",
            "type": "user_message",
            "input": "Survives restart",
            "output": "Survives restart",
            "createdAt": "2026-09-27T11:00:00Z",
        })

        # Close first data layer instance
        await self.dl.close()

        # Reopen a fresh instance pointing to the same SQLite database file
        new_dl = SQLiteDataLayer(db_path=self.db_path)
        try:
            thread = await new_dl.get_thread(thread_id)
            self.assertIsNotNone(thread)
            self.assertEqual(thread["name"], "Restart Survival Test")
            self.assertEqual(len(thread["steps"]), 1)
            self.assertEqual(thread["steps"][0]["output"], "Survives restart")
        finally:
            await new_dl.close()

    async def test_listing_and_pagination(self):
        # Create 5 conversations with different timestamps
        for i in range(1, 6):
            t_id = f"thread-page-{i}"
            created = f"2026-09-27T10:0{i}:00Z"
            await self.dl.update_thread(thread_id=t_id, name=f"Investigation {i}", user_id="local-user")
            await self.dl.create_step({
                "id": f"step-p-{i}",
                "threadId": t_id,
                "name": "user",
                "type": "user_message",
                "input": f"Question {i}",
                "output": f"Answer {i}",
                "createdAt": created,
            })

        # First page with limit 2
        p1 = await self.dl.list_threads(
            pagination=Pagination(first=2, cursor=None),
            filters=ThreadFilter(userId="local-user")
        )
        self.assertEqual(len(p1.data), 2)
        self.assertTrue(p1.pageInfo.hasNextPage)
        # Investigation 5 is the most recent
        self.assertEqual(p1.data[0]["id"], "thread-page-5")
        self.assertEqual(p1.data[1]["id"], "thread-page-4")

        # Second page using endCursor
        cursor = p1.pageInfo.endCursor
        self.assertEqual(cursor, "thread-page-4")

        p2 = await self.dl.list_threads(
            pagination=Pagination(first=2, cursor=cursor),
            filters=ThreadFilter(userId="local-user")
        )
        self.assertEqual(len(p2.data), 2)
        self.assertTrue(p2.pageInfo.hasNextPage)
        self.assertEqual(p2.data[0]["id"], "thread-page-3")
        self.assertEqual(p2.data[1]["id"], "thread-page-2")

        # Third page
        p3 = await self.dl.list_threads(
            pagination=Pagination(first=2, cursor=p2.pageInfo.endCursor),
            filters=ThreadFilter(userId="local-user")
        )
        self.assertEqual(len(p3.data), 1)
        self.assertFalse(p3.pageInfo.hasNextPage)
        self.assertEqual(p3.data[0]["id"], "thread-page-1")

    async def test_conversation_search(self):
        # Thread 1: AKS ImagePullBackOff
        await self.dl.update_thread(thread_id="t-aks", name="AKS Registry Error", user_id="local-user")
        await self.dl.create_step({
            "id": "s-aks-1",
            "threadId": "t-aks",
            "name": "user",
            "type": "user_message",
            "input": "Diagnose image pull issue",
            "output": "ImagePullBackOff for tag v2.0.0",
            "createdAt": "2026-09-27T12:00:00Z",
        })

        # Thread 2: Azure DevOps Pipeline
        await self.dl.update_thread(thread_id="t-ado", name="ADO Pipeline Review", user_id="local-user")
        await self.dl.create_step({
            "id": "s-ado-1",
            "threadId": "t-ado",
            "name": "user",
            "type": "user_message",
            "input": "Review azure-pipelines.yml",
            "output": "Trigger config is missing branch filter",
            "createdAt": "2026-09-27T12:05:00Z",
        })

        # Search for "Registry" (matches thread 1 name)
        res_reg = await self.dl.list_threads(
            pagination=Pagination(first=10),
            filters=ThreadFilter(search="Registry", userId="local-user")
        )
        self.assertEqual(len(res_reg.data), 1)
        self.assertEqual(res_reg.data[0]["id"], "t-aks")

        # Search for "ImagePullBackOff" (matches thread 1 step output)
        res_img = await self.dl.list_threads(
            pagination=Pagination(first=10),
            filters=ThreadFilter(search="imagepullbackoff", userId="local-user")
        )
        self.assertEqual(len(res_img.data), 1)
        self.assertEqual(res_img.data[0]["id"], "t-aks")

        # Search for "pipelines.yml" (matches thread 2 step input)
        res_pipe = await self.dl.list_threads(
            pagination=Pagination(first=10),
            filters=ThreadFilter(search="pipelines.yml", userId="local-user")
        )
        self.assertEqual(len(res_pipe.data), 1)
        self.assertEqual(res_pipe.data[0]["id"], "t-ado")

        # Search with no match
        res_none = await self.dl.list_threads(
            pagination=Pagination(first=10),
            filters=ThreadFilter(search="NonexistentServiceXYZ", userId="local-user")
        )
        self.assertEqual(len(res_none.data), 0)

    async def test_conversation_deletion(self):
        thread_id = "thread-delete-004"
        await self.dl.update_thread(thread_id=thread_id, name="To Delete", user_id="local-user")
        await self.dl.create_step({
            "id": "step-del-1",
            "threadId": thread_id,
            "name": "user",
            "type": "user_message",
            "input": "Delete me",
            "output": "Delete me",
            "createdAt": "2026-09-27T13:00:00Z",
        })
        await self.dl.create_element({
            "id": "elem-del-1",
            "threadId": thread_id,
            "name": "Side Note",
            "type": "text",
            "content": "Temporary content",
        })

        # Verify exists
        self.assertIsNotNone(await self.dl.get_thread(thread_id))

        # Delete thread
        await self.dl.delete_thread(thread_id)

        # Verify deleted
        self.assertIsNone(await self.dl.get_thread(thread_id))
        self.assertIsNone(await self.dl.get_element(thread_id, "elem-del-1"))

    async def test_opening_and_continuing_conversation(self):
        """Test resuming an existing thread and continuing with subsequent messages."""
        thread_id = "thread-resume-005"
        await self.dl.update_thread(thread_id=thread_id, name="Continuous Investigation", user_id="local-user")

        # Message 1
        await self.dl.create_step({
            "id": "step-c1",
            "threadId": thread_id,
            "name": "user",
            "type": "user_message",
            "input": "First question: list pods",
            "output": "First question: list pods",
            "createdAt": "2026-09-27T14:00:00Z",
        })
        await self.dl.create_step({
            "id": "step-c2",
            "threadId": thread_id,
            "name": "agent",
            "type": "assistant_message",
            "input": "",
            "output": "Found 3 pods.",
            "createdAt": "2026-09-27T14:00:05Z",
        })

        # Resume: fetch thread history
        resumed = await self.dl.get_thread(thread_id)
        self.assertEqual(len(resumed["steps"]), 2)

        # Continuation: Message 2 in the same thread
        await self.dl.create_step({
            "id": "step-c3",
            "threadId": thread_id,
            "name": "user",
            "type": "user_message",
            "input": "Second question: check logs for pod 1",
            "output": "Second question: check logs for pod 1",
            "createdAt": "2026-09-27T14:05:00Z",
        })
        await self.dl.create_step({
            "id": "step-c4",
            "threadId": thread_id,
            "name": "agent",
            "type": "assistant_message",
            "input": "",
            "output": "Log excerpt: Error connecting to DB.",
            "createdAt": "2026-09-27T14:05:10Z",
        })

        # Fetch thread again: now has 4 steps in exact chronological sequence
        updated = await self.dl.get_thread(thread_id)
        self.assertEqual(len(updated["steps"]), 4)
        self.assertEqual([s["id"] for s in updated["steps"]], ["step-c1", "step-c2", "step-c3", "step-c4"])


if __name__ == "__main__":
    unittest.main()
