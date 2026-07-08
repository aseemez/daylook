"""Generate invite tokens for the relay.

    python gen_tokens.py 50

Prints one token per line. Keep the name->token mapping somewhere private
(a sheet is fine); the tokens themselves go into the relay's INVITE_TOKENS
env var, comma-separated. Rotate someone off by removing their token and
redeploying.
"""

import secrets
import sys

count = int(sys.argv[1]) if len(sys.argv) > 1 else 50
for _ in range(count):
    print(f"cl_{secrets.token_urlsafe(12)}")
