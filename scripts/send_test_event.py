"""Post one signed `payout.failed` event to the local app, with no Stripe involved.

Run: STRIPE_WEBHOOK_SECRET=whsec_demo uv run python scripts/send_test_event.py [payout_id]
The app must run with the same STRIPE_WEBHOOK_SECRET. The event is built by hand in Stripe's
shape; it is signed with Stripe's real scheme, so the app checks it like a real one.
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

payout_id = sys.argv[1] if len(sys.argv) > 1 else f"po_demo_{int(time.time())}"
secret = os.environ["STRIPE_WEBHOOK_SECRET"]
url = f"http://localhost:{os.environ.get('PORT', '8000')}/webhooks/stripe"
payload = json.dumps(
    {
        "id": f"evt_{payout_id}",
        "object": "event",
        "type": "payout.failed",
        "account": "acct_demo_seller",
        "data": {"object": {"id": payout_id, "object": "payout"}},
    }
)
timestamp = int(time.time())
signature = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256)
request = urllib.request.Request(
    url,
    data=payload.encode(),
    headers={
        "Content-Type": "application/json",
        "Stripe-Signature": f"t={timestamp},v1={signature.hexdigest()}",
    },
)
with urllib.request.urlopen(request) as response:
    print(f"sent {payout_id}: HTTP {response.status}")
