# Merge these settings into the generated demo/settings.py.
import os

MIDDLEWARE += [
    "apirelio_django.ApirelioMiddleware",
]
APIRELIO = {
    "API_KEY": os.environ.get("APIRELIO_API_KEY", ""),
    "SERVICE": "github-quickstart",
    "ENVIRONMENT": "development",
    "INCLUDE_ROUTES": ("/api/**",),
    "RESOLVE_CUSTOMER": lambda _request: {
        "id": "customer_42", "name": "Acme Europe", "plan": "growth"
    },
}

