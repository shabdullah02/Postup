import os
import re

from article_publisher.gemini_client import MODEL_NAME
from google.genai import types


def generate_post_text(topic: str, gemini_api_key: str | None = None) -> str:
    from article_publisher import exceptions
    from article_publisher import generate_article

    text = generate_article(topic)
    body, refs = (text.split("References", 1) + [""])[:2]
    source_url = None
    matches = re.finditer(r"https?://[^\s\[\]()|]+", refs)
    for match in matches:
        candidate = match.group(0)
        if len(candidate) > 15:
            source_url = candidate
            break

    import article_publisher
    from google.genai import Client

    client_factory = getattr(article_publisher, "get_client", None)
    if callable(client_factory):
        client = client_factory(gemini_api_key)
    else:
        client = Client(api_key=gemini_api_key or os.environ["GEMINI_API_KEY"])

    instruction = (
        "Rewrite as a Facebook post, 60-110 words, plain text only. First line is a "
        "one-sentence hook. Then 2-3 short paragraphs separated by blank lines. No "
        "markdown, no citation markers like [1], no emojis, no headings. End with 2-3 "
        "hashtags on the last line. Use only facts present in the text. Add no new facts, "
        "numbers, or names."
    )

    if hasattr(client, "models"):
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=body,
            config=types.GenerateContentConfig(system_instruction=instruction),
        )
    elif hasattr(client, "generate_content"):
        response = client.generate_content(body, instruction)
    else:
        response = client.generate_content(
            model=MODEL_NAME,
            contents=body,
            config=types.GenerateContentConfig(system_instruction=instruction),
        )

    result = getattr(response, "text", response)
    if not result or not result.strip():
        raise exceptions.GenerationError("Gemini returned empty text.")

    result = re.sub(r"\[[^\]]+\]", "", result)
    result = re.sub(r"(^|\n)#+\s*", "\\1", result)
    result = re.sub(r"[*_`]", "", result)
    result = result.strip()
    if source_url:
        result = f"{result}\n\n{source_url}"
    return result
