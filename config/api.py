from rest_framework.views import exception_handler
from rest_framework.response import Response


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(response.data, dict) and "detail" in response.data:
        detail = response.data["detail"]
    else:
        detail = response.data

    code = "validation_error" if response.status_code == 400 else None
    if code is None and hasattr(exc, "get_codes"):
        exception_codes = exc.get_codes()
        if isinstance(exception_codes, str):
            code = exception_codes
    if code is None:
        code = getattr(exc, "default_code", None)
    if not code:
        code = {
            401: "not_authenticated",
            403: "permission_denied",
            404: "not_found",
            429: "throttled",
        }.get(response.status_code, "error")
    response.data = {"detail": detail, "code": str(code)}
    return response


def error_response(detail, code, status_code):
    return Response(
        {"detail": detail, "code": code},
        status=status_code,
    )