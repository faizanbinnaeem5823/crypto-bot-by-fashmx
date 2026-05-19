#!/usr/bin/env python3
"""Generate Ed25519 keypair for Binance API.

Usage::

    python scripts/generate_ed25519_keys.py

Output:
    - Saves private key to ``keys/ed25519_private.pem``
    - Prints public key to stdout (give this to Binance)

Security:
    - Private key file is chmod 600 (only owner can read).
    - Never commit the ``keys/`` directory.
"""

import os
import sys
from pathlib import Path

# Add project root to path so we can import the client
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.exchange.binance_client import BinanceClient  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("  Generating Ed25519 Keypair for Binance API")
    print("=" * 60)

    private_pem, public_pem = BinanceClient.generate_ed25519_keypair()

    # Create keys directory
    keys_dir = _PROJECT_ROOT / "keys"
    keys_dir.mkdir(exist_ok=True)

    # Write .gitignore if it doesn't exist
    gitignore = keys_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n!.gitignore\n")

    # Save private key with restrictive permissions
    private_path = keys_dir / "ed25519_private.pem"
    with open(private_path, "w", encoding="utf-8") as fh:
        fh.write(private_pem)
    os.chmod(private_path, 0o600)  # owner read/write only

    print(f"\nPrivate key saved to : {private_path}")
    print("WARNING:  Keep this file secret!  Never commit it to git!\n")

    print("=" * 60)
    print("  PUBLIC KEY  —  paste into Binance API Management")
    print("=" * 60)
    print(public_pem)

    print("\n" + "=" * 60)
    print("  NEXT STEPS")
    print("=" * 60)
    print("  1. Copy the PUBLIC KEY above.")
    print("  2. Go to Binance → API Management → Create API Key.")
    print("  3. Select 'Ed25519' as the key type.")
    print("  4. Paste the public key.")
    print("  5. Store API key in .env:   BINANCE_API_KEY=<key>")
    print(f"  6. Store private key path:  BINANCE_PRIVATE_KEY_PATH={private_path}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
