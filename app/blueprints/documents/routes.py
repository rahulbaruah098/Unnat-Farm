import os

from flask import Blueprint, abort, request, send_file

from app.services.document_service import candidate_upload_dirs, find_document_path
from app.utils.decorators import login_required


documents_bp = Blueprint('documents', __name__, url_prefix='/documents')


# Backward-compatible private helper names. Dashboard and older routes import
# these names directly, so keep them while using one resolver implementation.
def _candidate_upload_dirs():
    return candidate_upload_dirs()


def _find_document_path(filename):
    return find_document_path(filename)


@documents_bp.route('/<path:filename>')
@login_required
def serve(filename):
    file_path = find_document_path(filename)
    if not file_path:
        abort(404)

    as_attachment = request.args.get('download') == '1'
    return send_file(
        file_path,
        as_attachment=as_attachment,
        download_name=os.path.basename(file_path),
        conditional=True,
    )
