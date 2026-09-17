"""Retry Gemini HTTP 503 service-unavailable responses only."""

import asyncio

from google.genai.errors import APIError


async def generate_with_unavailable_retry(generate_content, **request):
    """Reuse the same request objects for at most three SDK attempts."""
    for attempt in range(3):
        try:
            return await generate_content(**request)
        except APIError as error:
            if error.code != 503 or attempt == 2:
                raise
            delay = 3 * (attempt + 1)
            print(
                f"Gemini temporarily unavailable. Retrying in {delay} seconds...",
                flush=True,
            )
            await asyncio.sleep(delay)
