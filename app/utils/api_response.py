from flask import g, jsonify


def api_success(data=None, status=200, meta=None):
    payload = {
        "ok": True,
        "data": {} if data is None else data,
    }
    if meta is not None:
        payload["meta"] = meta
    response = jsonify(payload)
    response.status_code = status
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response


def api_error(code, message, status=400, details=None):
    error = {
        "code": str(code or "API_ERROR"),
        "message": str(message or "Request failed."),
    }
    if details is not None:
        error["details"] = details

    response = jsonify({
        "ok": False,
        "error": error,
    })
    response.status_code = status
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response
