"""One-shot smoke test for the active API provider's key.

Run before launching serve.py to confirm:

  1. The key is reachable for the active provider (env var or config file).
  2. The key is valid against the provider's API (we hit a 1-token
     request on the two configured models to verify auth + connectivity).
  3. The two model ids the plugin uses are recognised by the provider
     (so a typo or catalog change surfaces here instead of during a
     live multi-agent boot).

Active provider is controlled by ``SAGENT_API_PROVIDER`` (default
``google``; set to ``anthropic`` to switch). Provider + model
constants live in ``roles/common.py``.

Usage::

    cd plugin/blackjax_chat_sagent_api_v3
    uv run python bin/check_api_key.py

Exits non-zero on any failure with a specific diagnostic. Cost of
a successful run: ~$0.0001 — two tiny prompts at flash rates.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make ``roles/common.py`` importable when running this script directly.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_ROOT))


async def _smoke_test_model(provider_obj: object, model_id: str) -> None:
    """Hit ``model_id`` with a 1-token prompt to verify auth + reachability."""
    from sagent.types.model import ModelRequest
    from sagent.types.runtime import UserMessage

    model = provider_obj.model(model_id)
    request = ModelRequest(
        system="Respond with the single word: ok",
        messages=(UserMessage(text="ping"),),
        tools=(),
    )
    print(f"[..] Calling {model_id} with a 1-token prompt...")
    response = await model.stream(request, on_text=None, on_thinking=None)
    text = (response.message.text or "").strip()
    cost = getattr(response, "total_cost", 0.0)
    print(f"[ok] {model_id} responded: {text!r} (cost: ${cost:.6f})")


async def main() -> None:
    # Import here so the SAGENT_API_PROVIDER env var resolved at
    # ``common.py`` import time reflects the operator's current shell.
    from roles import common

    print(f"=== {common._PROVIDER} API key smoke test ===\n")
    env_var, config_path, signup_url = common._API_KEY_SOURCES[common._PROVIDER]

    # Report key source explicitly so the operator sees which path
    # succeeded (helps when they wonder which key the system is using).
    if os.environ.get(env_var, "").strip():
        print(f"[ok] Found key in {env_var} env var.")
    elif config_path.exists() and config_path.read_text(
        encoding="utf-8",
    ).strip():
        print(f"[ok] Found key in {config_path}.")
    else:
        print(f"[err] No key found. Set {env_var} env var OR write "
              f"the key to {config_path}.")
        print(f"      Get a key at: {signup_url}")
        sys.exit(2)

    try:
        provider_obj = common.build_provider()
    except RuntimeError as exc:
        print(f"[err] build_provider() failed: {exc}")
        sys.exit(2)

    # Test both models that this plugin uses (TL + default tier).
    for mid in (common.MODEL_TL, common.MODEL_DEFAULT):
        try:
            await _smoke_test_model(provider_obj, mid)
        except Exception as exc:  # noqa: BLE001 -- surface to operator
            print(f"[err] {mid} call failed: {type(exc).__name__}: {exc}")
            print(f"      If this says 'API_KEY_INVALID' or '403', the "
                  f"key didn't authenticate.")
            print(f"      If this says 'model not found', the catalog "
                  f"may be out of date for this sagent version.")
            sys.exit(1)
    print("\n=== All checks passed; key + models are good. ===")
    print("Run: SAGENT_DATA_DIR=<dir> python bin/serve.py --port 8767")


if __name__ == "__main__":
    asyncio.run(main())
