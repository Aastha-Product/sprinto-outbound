"""Print the Gemini models your API key can use for text generation.

Run:  python scripts\\list_models.py
Then copy one "flash" model name into GEMINI_MODEL= in your .env file.
"""

import _bootstrap  # noqa: F401  (sets up imports + UTF-8 console)

import sys

from outbound.config import ConfigError, get_settings
from outbound.llm import get_client


def main() -> int:
    try:
        get_settings().require("gemini_key")
        models = list(get_client().models.list())
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 1
    except Exception as exc:  # network / bad key: show the type, never the key
        print(f"❌ Could not list models ({type(exc).__name__}): {exc}")
        return 1

    usable = [m for m in models if "generateContent" in (m.supported_actions or [])]
    usable.sort(key=lambda m: m.name or "")
    print(f"{len(usable)} models support generateContent:\n")
    for m in usable:
        short = (m.name or "").removeprefix("models/")
        tag = "  <- flash" if "flash" in short and "image" not in short and "tts" not in short else ""
        print(f"  {short:45s} {m.display_name or ''}{tag}")
    print("\nCopy one of the '<- flash' names into GEMINI_MODEL= in .env (no quotes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
