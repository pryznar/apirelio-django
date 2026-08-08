# Apirelio Django SDK

[Documentation](https://apirelio.com/docs/python/django) · [PyPI](https://pypi.org/project/apirelio-django/) · [Apirelio](https://apirelio.com)

> Connect Django and DRF API errors, latency and releases to the affected customers without capturing request or response payloads.

Native Django middleware for privacy-safe, customer-aware API analytics. The same package supports
regular Django views and Django REST Framework endpoints.

```bash
pip install apirelio-django
```

Add the middleware after Django's authentication middleware so identity resolvers can use
`request.user`:

```python
# settings.py
import os

MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apirelio_django.ApirelioMiddleware",
]

APIRELIO = {
    "API_KEY": os.environ.get("APIRELIO_API_KEY", ""),
    "ENDPOINT": os.environ.get("APIRELIO_ENDPOINT", "https://apirelio.com"),
    "SERVICE": "billing-api",
    "ENVIRONMENT": "production",
    "INCLUDE_ROUTES": ("/api/**",),
    "EXCLUDE_ROUTES": ("/api/health/", "/api/internal/**"),
    "RESOLVE_CUSTOMER": "billing.apirelio.resolve_customer",
}
```

Resolvers receive the completed `HttpRequest` and may return an Apirelio customer, application or
metadata dictionary:

```python
def resolve_customer(request):
    account = getattr(request.user, "account", None)
    if account is None:
        return None
    return {"id": str(account.id), "name": account.name, "plan": account.plan}
```

The middleware records the final status, duration, resolved URL pattern and DRF error code. Both
synchronous and asynchronous Django handlers are supported. Events enter the bounded Python Core
background queue and are flushed when the process exits. Bodies, query strings, cookies,
authorization values, email addresses and client IP addresses are never captured.
