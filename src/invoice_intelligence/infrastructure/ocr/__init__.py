"""Infrastructure adapters for independent OCR providers."""

from .paddlex_http import PaddleXOCRHttpAdapter

__all__ = ["PaddleXOCRHttpAdapter"]
