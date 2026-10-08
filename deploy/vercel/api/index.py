"""Vercel entry point for the lookup API; public/index.html is the lookup page.

Vercel runs this project's root directory (deploy/vercel/api) as one Python function and
serves public/ from its CDN. Settings come from the project's environment variables:
DATABASE_URL and CLIENT_IP_HEADER=X-Real-IP (Vercel overwrites that header on every
request, so a client cannot pick its own address).
"""

from tripvane_api.app import create_app

app = create_app()
