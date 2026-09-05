from math import ceil

from flask import request

from app.utils.api_errors import ApiValidationError
from app.utils.api_serializers import serialize_document


DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100


def parse_pagination(default_per_page=DEFAULT_PER_PAGE, max_per_page=MAX_PER_PAGE):
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", default_per_page))
    except (TypeError, ValueError):
        raise ApiValidationError(
            "page and per_page must be integers.",
            code="INVALID_PAGINATION",
        )

    if page < 1:
        raise ApiValidationError("page must be at least 1.", code="INVALID_PAGE")
    if per_page < 1 or per_page > max_per_page:
        raise ApiValidationError(
            f"per_page must be between 1 and {max_per_page}.",
            code="INVALID_PER_PAGE",
        )
    return page, per_page


def pagination_meta(page, per_page, total):
    total = max(int(total or 0), 0)
    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": int(ceil(total / per_page)) if total else 0,
    }


def paginate_collection(collection, query=None, *, projection=None, sort=None, serializer=None):
    query = query or {}
    page, per_page = parse_pagination()
    total = collection.count_documents(query)

    cursor = collection.find(query, projection)
    if sort:
        cursor = cursor.sort(sort)
    cursor = cursor.skip((page - 1) * per_page).limit(per_page)

    serializer = serializer or serialize_document
    items = [serializer(item) for item in cursor]
    return {
        "items": items,
        "pagination": pagination_meta(page, per_page, total),
    }
