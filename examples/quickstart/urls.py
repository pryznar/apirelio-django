from django.http import JsonResponse
from django.urls import path

urlpatterns = [
    path(
        "api/invoices/<str:invoice_id>",
        lambda _request, invoice_id: JsonResponse({"id": invoice_id, "status": "paid"}),
    ),
]
