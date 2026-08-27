from __future__ import annotations

"""Environment-variable compatibility helpers.

The project uses explicit ``DEEPSEEK_*`` and ``ARK_*`` names as its stable
configuration contract. A few local OpenCode/Ark setups expose the same
credentials through ``ANTHROPIC_*`` compatibility names, so adapters may
read those names only as fallbacks. The helper intentionally returns the
first non-empty value and never logs it.
"""

import os


def env_first(*names: str, default: str = "") -> str:
    """Return the first non-empty environment value among *names*."""

    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default
