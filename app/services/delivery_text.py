"""Plain-text presentation for generated answers, not user-authored literals."""
import re


def plain_delivery_text(text: str) -> str:
    # Keep code contents and URLs; remove only paired presentation markers.
    text = re.sub(r"(?m)^\s*```[^\n]*\n?", "", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", text)
    for marker in ("**", "__"):
        text = re.sub(re.escape(marker) + r"([^\n]+?)" + re.escape(marker), r"\1", text)
    text = re.sub(r"(?m)^ {0,3}#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^([ \t]*)[-*+]\s+", r"\1• ", text)
    return text.strip()
