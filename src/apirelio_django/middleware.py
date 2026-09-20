import atexit
import inspect
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Optional, TypeVar, Union, cast

from apirelio import (
    ApirelioClient,
    Environment,
    EventTransport,
    Metadata,
    normalize_route,
    should_capture_route,
)
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.http import HttpRequest
from django.http.response import HttpResponseBase
from django.utils.module_loading import import_string

SDK_VERSION = "0.2.1"
ResolverValue = TypeVar("ResolverValue")
Resolver = Callable[[HttpRequest], Optional[ResolverValue]]
ErrorCodeResolver = Callable[
    [HttpRequest, Optional[BaseException], Optional[HttpResponseBase]], Optional[str]
]
GetResponse = Callable[
    [HttpRequest], Union[HttpResponseBase, Awaitable[HttpResponseBase]]
]

_CONVERTER = re.compile(r"<(?:(?:[^>:]+):)?([^>]+)>")
_REQUEST_ERROR_ATTRIBUTE = "_apirelio_exception"
_clients: list[ApirelioClient] = []


class ApirelioMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: GetResponse,
        *,
        client: Optional[ApirelioClient] = None,
        config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

        options = dict(config if config is not None else _settings_config())
        configured_client = options.get("CLIENT")
        self.client = client or (
            configured_client
            if isinstance(configured_client, ApirelioClient)
            else _create_client(options)
        )
        _clients.append(self.client)

        self.include_routes = _sequence(options.get("INCLUDE_ROUTES"))
        self.exclude_routes = _sequence(options.get("EXCLUDE_ROUTES"))
        self.resolve_customer = _resolver(options.get("RESOLVE_CUSTOMER"))
        self.resolve_application = _resolver(options.get("RESOLVE_APPLICATION"))
        self.resolve_error_code = _error_resolver(options.get("RESOLVE_ERROR_CODE"))
        self.resolve_metadata = _resolver(options.get("RESOLVE_METADATA"))
        self.api_version_header = str(
            options.get("API_VERSION_HEADER", "x-api-version")
        ).lower()
        self.capture_headers = tuple(
            name.lower()
            for name in _sequence(
                options.get("CAPTURE_HEADERS", ("x-sdk-version", "user-agent"))
            )
        )

    def __call__(
        self, request: HttpRequest
    ) -> Union[HttpResponseBase, Awaitable[HttpResponseBase]]:
        if self._is_async:
            return self.__acall__(request)
        return self.__scall__(request)

    def __scall__(self, request: HttpRequest) -> HttpResponseBase:
        started_at = time.perf_counter()
        response: Optional[HttpResponseBase] = None
        error: Optional[BaseException] = None
        try:
            result = self.get_response(request)
            if inspect.isawaitable(result):
                raise TypeError(
                    "An async Django handler was used in a synchronous middleware chain"
                )
            response = result
            return response
        except BaseException as caught:
            error = caught
            raise
        finally:
            self._capture(request, response, error, started_at)

    async def __acall__(self, request: HttpRequest) -> HttpResponseBase:
        started_at = time.perf_counter()
        response: Optional[HttpResponseBase] = None
        error: Optional[BaseException] = None
        try:
            result = self.get_response(request)
            response = await result if inspect.isawaitable(result) else result
            return response
        except BaseException as caught:
            error = caught
            raise
        finally:
            self._capture(request, response, error, started_at)

    def process_exception(
        self, request: HttpRequest, exception: BaseException
    ) -> None:
        setattr(request, _REQUEST_ERROR_ATTRIBUTE, exception)

    def _capture(
        self,
        request: HttpRequest,
        response: Optional[HttpResponseBase],
        error: Optional[BaseException],
        started_at: float,
    ) -> None:
        try:
            route = _route_template(request)
            if not should_capture_route(route, self.include_routes, self.exclude_routes):
                return

            request_error = error or cast(
                Optional[BaseException],
                getattr(request, _REQUEST_ERROR_ATTRIBUTE, None),
            )
            headers = request.headers
            metadata = _captured_headers(headers.items(), self.capture_headers)
            resolved_metadata = _resolve_safely(self.resolve_metadata, request)
            if resolved_metadata:
                metadata.update(resolved_metadata)

            self.client.capture(
                {
                    "method": request.method or "UNKNOWN",
                    "route": route,
                    "route_name": _route_name(request),
                    "status": response.status_code if response is not None else 500,
                    "duration_ms": (time.perf_counter() - started_at) * 1000,
                    "request_bytes": _string_integer(headers.get("content-length")),
                    "response_bytes": _response_bytes(response),
                    "customer": _resolve_safely(self.resolve_customer, request),
                    "application": _resolve_safely(self.resolve_application, request),
                    "api_version": headers.get(self.api_version_header),
                    "sdk": "django",
                    "sdk_version": SDK_VERSION,
                    "error_code": self._error_code(request, request_error, response),
                    "metadata": metadata,
                }
            )
        except BaseException:
            # Analytics must never alter the Django response or exception.
            pass

    def _error_code(
        self,
        request: HttpRequest,
        error: Optional[BaseException],
        response: Optional[HttpResponseBase],
    ) -> Optional[str]:
        if self.resolve_error_code:
            try:
                resolved = self.resolve_error_code(request, error, response)
                if resolved:
                    return str(resolved)[:255]
            except BaseException:
                pass
        if error:
            return error.__class__.__name__[:255]
        if response is not None and response.status_code >= 400:
            return _drf_error_code(getattr(response, "data", None))
        return None


def get_client() -> Optional[ApirelioClient]:
    return _clients[-1] if _clients else None


def shutdown_apirelio(timeout: float = 5.0) -> bool:
    succeeded = True
    while _clients:
        client = _clients.pop()
        try:
            succeeded = client.shutdown(timeout) and succeeded
        except BaseException:
            succeeded = False
    return succeeded


def _settings_config() -> Mapping[str, Any]:
    value = getattr(settings, "APIRELIO", {})
    return value if isinstance(value, Mapping) else {}


def _create_client(options: Mapping[str, Any]) -> ApirelioClient:
    return ApirelioClient(
        api_key=str(options.get("API_KEY", "")),
        service=str(options.get("SERVICE", "django-api")),
        endpoint=str(options.get("ENDPOINT", "https://apirelio.com")),
        environment=cast(Environment, options.get("ENVIRONMENT", "production")),
        release=_optional_string(options.get("RELEASE")),
        enabled=bool(options.get("ENABLED", True)),
        batch_size=_integer(options.get("BATCH_SIZE"), 100),
        flush_interval=_number(options.get("FLUSH_INTERVAL"), 5.0),
        max_queue_size=_integer(options.get("MAX_QUEUE_SIZE"), 10_000),
        timeout=_number(options.get("TIMEOUT"), 2.0),
        max_retries=_integer(options.get("MAX_RETRIES"), 2),
        metadata_keys=_sequence(options.get("METADATA_KEYS")),
        transport=cast(Optional[EventTransport], options.get("TRANSPORT")),
    )


def _resolver(value: Any) -> Optional[Resolver[Any]]:
    resolved = _callable(value)
    return cast(Optional[Resolver[Any]], resolved)


def _error_resolver(value: Any) -> Optional[ErrorCodeResolver]:
    resolved = _callable(value)
    return cast(Optional[ErrorCodeResolver], resolved)


def _callable(value: Any) -> Optional[Callable[..., Any]]:
    try:
        resolved = import_string(value) if isinstance(value, str) else value
        return resolved if callable(resolved) else None
    except BaseException:
        return None


def _resolve_safely(
    resolver: Optional[Resolver[ResolverValue]], request: HttpRequest
) -> Optional[ResolverValue]:
    try:
        return resolver(request) if resolver else None
    except BaseException:
        return None


def _route_template(request: HttpRequest) -> str:
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None)
    if isinstance(route, str) and route:
        route = _CONVERTER.sub(r"{\1}", route).lstrip("^")
        return normalize_route("/" + route.lstrip("/"))
    return normalize_route(request.path_info)


def _route_name(request: HttpRequest) -> Optional[str]:
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "view_name", None)
    return str(name)[:255] if name else None


def _captured_headers(
    headers: Iterable[tuple[str, str]], names: Sequence[str]
) -> Metadata:
    allowed = set(names)
    return {
        "header." + key.lower(): value
        for key, value in headers
        if key.lower() in allowed
    }


def _response_bytes(response: Optional[HttpResponseBase]) -> Optional[int]:
    if response is None:
        return None
    return _string_integer(response.headers.get("content-length"))


def _drf_error_code(value: Any, depth: int = 0) -> Optional[str]:
    if value is None or depth > 4:
        return None
    code = getattr(value, "code", None)
    if isinstance(code, str) and code:
        return code[:255]
    if isinstance(value, Mapping):
        ordered = list(value.values())
        for item in ordered:
            resolved = _drf_error_code(item, depth + 1)
            if resolved:
                return resolved
    elif isinstance(value, (list, tuple)):
        for item in value:
            resolved = _drf_error_code(item, depth + 1)
            if resolved:
                return resolved
    return None


def _sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return ()


def _optional_string(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _string_integer(value: Optional[str]) -> Optional[int]:
    return int(value) if value and value.isdigit() else None


atexit.register(shutdown_apirelio)
