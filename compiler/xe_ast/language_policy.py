"""Explicit language profiles; the unreviewed historical compiler is never implicit.

Context-local state keeps nested tests and concurrent callers independent.  There
is deliberately no environment variable, pathname or test-runner exemption.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_PROFILE: ContextVar[str] = ContextVar("xe_language_profile", default="core")
_PROFILES = frozenset({"core", "legacy"})


def current_profile() -> str:
    """Return the explicitly selected profile, defaulting to the reviewed core."""
    return _PROFILE.get()


@contextmanager
def language_profile(profile: str) -> Iterator[None]:
    """Temporarily select a profile and restore it even when compilation fails."""
    if profile not in _PROFILES:
        raise ValueError(f"unknown Xe language profile: {profile!r}")
    token = _PROFILE.set(profile)
    try:
        yield
    finally:
        _PROFILE.reset(token)
