import asyncio
import os
from collections.abc import Sequence
from typing import NoReturn

import django
from apirelio import ApirelioEvent

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")
django.setup()

from django.http import HttpRequest, JsonResponse  # noqa: E402
from django.test import AsyncClient, Client, override_settings  # noqa: E402
from django.urls import path  # noqa: E402
from rest_framework.exceptions import ValidationError  # noqa: E402
from rest_framework.request import Request  # noqa: E402
from rest_framework.response import Response  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

from apirelio_django import get_client, shutdown_apirelio  # noqa: E402


class RecordingTransport:
    def __init__(self) -> None:
        self.events: list[ApirelioEvent] = []

    def send(self, events: Sequence[ApirelioEvent]) -> None:
        self.events.extend(events)


def customer_view(request: HttpRequest, customer_id: int) -> JsonResponse:
    return JsonResponse({"id": customer_id})


def internal_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


def explode_view(request: HttpRequest) -> NoReturn:
    raise ValueError("boom")


async def async_view(request: HttpRequest, item_id: int) -> JsonResponse:
    await asyncio.sleep(0)
    return JsonResponse({"id": item_id})


class InvalidItemView(APIView):
    def get(self, request: Request, item_id: int) -> Response:
        raise ValidationError("Invalid item", code="invalid_item")


urlpatterns = [
    path("customers/<int:customer_id>/", customer_view, name="customer-detail"),
    path("api/internal/", internal_view, name="internal"),
    path("explode/", explode_view, name="explode"),
    path("async-items/<int:item_id>/", async_view, name="async-item"),
    path("api/items/<int:item_id>/", InvalidItemView.as_view(), name="item-detail"),
]


def _config(transport: RecordingTransport, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "API_KEY": "apr_test",
        "SERVICE": "django-api",
        "ENVIRONMENT": "test",
        "TRANSPORT": transport,
    }
    config.update(overrides)
    return config


def _flush() -> None:
    client = get_client()
    assert client is not None
    assert client.flush()


@override_settings(MIDDLEWARE=["apirelio_django.ApirelioMiddleware"])
def test_captures_resolved_django_route_and_customer() -> None:
    transport = RecordingTransport()
    with override_settings(
        APIRELIO=_config(
            transport,
            RESOLVE_CUSTOMER=lambda request: {
                "id": request.headers.get("x-customer-id", "unknown"),
                "plan": "growth",
            },
        )
    ):
        response = Client().get(
            "/customers/42/?secret=nope", HTTP_X_CUSTOMER_ID="acme"
        )
        assert response.status_code == 200
        _flush()

    assert transport.events[0]["route"] == "/customers/{customer_id}/"
    assert transport.events[0]["route_name"] == "customer-detail"
    assert transport.events[0]["customer_id"] == "acme"
    assert transport.events[0]["sdk"] == "django"
    shutdown_apirelio()


@override_settings(MIDDLEWARE=["apirelio_django.ApirelioMiddleware"])
def test_filters_routes_and_captures_safe_metadata() -> None:
    transport = RecordingTransport()
    with override_settings(
        APIRELIO=_config(
            transport,
            INCLUDE_ROUTES=("/customers/**",),
            EXCLUDE_ROUTES=("/api/internal/**",),
            METADATA_KEYS=("region", "header.user-agent"),
            CAPTURE_HEADERS=("user-agent",),
            RESOLVE_METADATA=lambda request: {"region": "eu", "token": "never"},
        )
    ):
        browser = Client()
        browser.get("/customers/42/", HTTP_USER_AGENT="django-test")
        browser.get("/api/internal/")
        _flush()

    assert [event["route"] for event in transport.events] == [
        "/customers/{customer_id}/"
    ]
    assert transport.events[0]["metadata"] == {
        "region": "eu",
        "header.user-agent": "django-test",
    }
    shutdown_apirelio()


@override_settings(MIDDLEWARE=["apirelio_django.ApirelioMiddleware"])
def test_records_unhandled_exception_without_swallowing_it() -> None:
    transport = RecordingTransport()
    with override_settings(APIRELIO=_config(transport)):
        response = Client(raise_request_exception=False).get("/explode/")
        assert response.status_code == 500
        _flush()

    assert transport.events[0]["status"] == 500
    assert transport.events[0]["error_code"] == "ValueError"
    shutdown_apirelio()


@override_settings(MIDDLEWARE=["apirelio_django.ApirelioMiddleware"])
def test_captures_drf_error_code_without_reading_response_body() -> None:
    transport = RecordingTransport()
    with override_settings(APIRELIO=_config(transport)):
        response = Client().get("/api/items/42/")
        assert response.status_code == 400
        _flush()

    assert transport.events[0]["route"] == "/api/items/{item_id}/"
    assert transport.events[0]["error_code"] == "invalid_item"
    shutdown_apirelio()


@override_settings(MIDDLEWARE=["apirelio_django.ApirelioMiddleware"])
def test_supports_async_django_handlers() -> None:
    transport = RecordingTransport()

    async def request() -> None:
        with override_settings(APIRELIO=_config(transport)):
            response = await AsyncClient().get("/async-items/42/")
            assert response.status_code == 200
            _flush()

    asyncio.run(request())
    assert transport.events[0]["route"] == "/async-items/{item_id}/"
    shutdown_apirelio()


@override_settings(MIDDLEWARE=["apirelio_django.ApirelioMiddleware"])
def test_resolver_failure_never_changes_response() -> None:
    transport = RecordingTransport()

    def broken_resolver(request: HttpRequest) -> dict[str, str]:
        raise RuntimeError("resolver unavailable")

    with override_settings(
        APIRELIO=_config(transport, RESOLVE_CUSTOMER=broken_resolver)
    ):
        response = Client().get("/customers/42/")
        assert response.status_code == 200
        _flush()

    assert transport.events[0]["customer_id"] is None
    shutdown_apirelio()
