"""Vercel entry point for the collector (sensor ingest).

Vercel runs this project's root directory (deploy/vercel/collector) as one Python
function. Settings come from the project's environment variables: DATABASE_URL and
CANARY_HMAC_KEY.
"""

from tripvane_collector.app import create_app

app = create_app()
