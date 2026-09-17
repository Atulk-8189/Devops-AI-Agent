import unittest
from unittest.mock import AsyncMock, call, patch

from google.genai.errors import ClientError, ServerError

from src.review.gemini_retry import generate_with_unavailable_retry


def api_error(code):
    cls = ServerError if code >= 500 else ClientError
    return cls(code, {"error": {
        "code": code, "status": "UNAVAILABLE" if code == 503 else "OTHER",
        "message": "This model is currently experiencing high demand.",
    }})


class GeminiRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_without_retry(self):
        generate = AsyncMock(return_value="success")
        with patch("src.review.gemini_retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await generate_with_unavailable_retry(generate), "success")
            sleep.assert_not_awaited()
        generate.assert_awaited_once()

    async def test_503_then_success_preserves_input_and_logs(self):
        generate = AsyncMock(side_effect=[api_error(503), "success"])
        contents, config = ["same input"], {"same": "config"}
        with patch("src.review.gemini_retry.asyncio.sleep", new_callable=AsyncMock) as sleep, \
                patch("builtins.print") as output:
            result = await generate_with_unavailable_retry(
                generate, model="gemini-3.8-flash", contents=contents, config=config
            )
            self.assertEqual(result, "success")
            sleep.assert_awaited_once_with(3)
            output.assert_called_once_with(
                "Gemini temporarily unavailable. Retrying in 3 seconds...", flush=True
            )
        self.assertEqual(generate.await_count, 2)
        for attempt in generate.await_args_list:
            self.assertIs(attempt.kwargs["contents"], contents)
            self.assertIs(attempt.kwargs["config"], config)
            self.assertEqual(attempt.kwargs["model"], "gemini-3.8-flash")

    async def test_success_on_third_attempt(self):
        generate = AsyncMock(side_effect=[api_error(503), api_error(503), "success"])
        with patch("src.review.gemini_retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await generate_with_unavailable_retry(generate), "success")
            self.assertEqual(sleep.await_args_list, [call(3), call(6)])
        self.assertEqual(generate.await_count, 3)

    async def test_maximum_retries_reraises_original_error(self):
        failure = api_error(503)
        generate = AsyncMock(side_effect=failure)
        with patch("src.review.gemini_retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(ServerError) as caught:
                await generate_with_unavailable_retry(generate)
            self.assertIs(caught.exception, failure)
            self.assertEqual(sleep.await_args_list, [call(3), call(6)])
        self.assertEqual(generate.await_count, 3)

    async def test_non_503_errors_untouched(self):
        failures = [api_error(code) for code in (400, 401, 403, 404, 429, 500, 502, 504)]
        failures += [PermissionError("policy"), ValueError("schema/validation"),
                     RuntimeError("MCP 503 UNAVAILABLE"), TimeoutError("timeout")]
        for failure in failures:
            with self.subTest(error=type(failure).__name__, code=getattr(failure, "code", None)):
                generate = AsyncMock(side_effect=failure)
                with patch("src.review.gemini_retry.asyncio.sleep", new_callable=AsyncMock) as sleep, \
                        patch("builtins.print") as output:
                    with self.assertRaises(type(failure)) as caught:
                        await generate_with_unavailable_retry(generate)
                    self.assertIs(caught.exception, failure)
                    sleep.assert_not_awaited()
                    output.assert_not_called()
                generate.assert_awaited_once()
