"""OCR Protocol and Anthropic adapter."""

import base64
import logging
from typing import Protocol, runtime_checkable

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an expert handwriting OCR system. Extract all text from the provided "
    "handwritten image as accurately as possible. Preserve paragraph breaks and line "
    "structure. Output only the extracted text with no commentary or preamble."
)


@runtime_checkable
class OCRProvider(Protocol):
    """Protocol for OCR providers."""

    def extract_text(self, image_data: bytes, media_type: str) -> str: ...


class AnthropicOCRProvider:
    """OCR provider using Anthropic's Claude vision API."""

    def __init__(self, api_key: str, model: str, max_tokens: int) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def extract_text(self, image_data: bytes, media_type: str) -> str:
        """Extract text from an image using Anthropic's vision API."""
        logger.info("Extracting text via Anthropic OCR (model=%s)", self._model)

        encoded_image = base64.standard_b64encode(image_data).decode("utf-8")

        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": encoded_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract all handwritten text from this image.",
                        },
                    ],
                }
            ],
        )

        extracted = message.content[0].text
        logger.info("OCR extraction complete (%d characters)", len(extracted))
        return extracted
