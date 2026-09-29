"""pilot_runtime.auth — the real Firebase Admin / Identity Platform binding."""

from .firebase_decoder import (
    FirebaseTokenDecoder,
    FirebaseInitError,
    initialize_firebase_app,
)

__all__ = [
    "FirebaseTokenDecoder",
    "FirebaseInitError",
    "initialize_firebase_app",
]
