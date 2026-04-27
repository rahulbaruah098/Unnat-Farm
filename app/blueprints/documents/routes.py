import os
from flask import Blueprint, send_file, current_app, abort, request
from werkzeug.utils import safe_join
from app.utils.decorators import login_required


documents_bp = Blueprint('documents', __name__, url_prefix='/documents')


def _candidate_upload_dirs():
    """Return all upload directories used by older/newer project versions.

    Earlier builds sometimes saved files under project_root/uploads, while some
    configs pointed to app/uploads or a relative uploads folder. This resolver
    checks all safe known locations so old uploaded documents continue to open
    from AVPL validation screens.
    """
    dirs = []

    configured = current_app.config.get('UPLOAD_FOLDER') or 'uploads'
    if configured:
        dirs.append(configured)
        if not os.path.isabs(configured):
            dirs.append(os.path.abspath(configured))
            dirs.append(os.path.abspath(os.path.join(current_app.root_path, '..', configured)))
            dirs.append(os.path.abspath(os.path.join(current_app.root_path, configured)))

    dirs.append(os.path.abspath(os.path.join(current_app.root_path, '..', 'uploads')))
    dirs.append(os.path.abspath(os.path.join(current_app.root_path, 'uploads')))

    # keep order, remove duplicates
    seen = set()
    clean = []
    for d in dirs:
        if not d:
            continue
        ad = os.path.abspath(d)
        if ad not in seen:
            seen.add(ad)
            clean.append(ad)
    return clean


def _find_document_path(filename):
    safe_name = os.path.basename(filename or '')
    if not safe_name or safe_name in {'.', '..'}:
        return None

    for directory in _candidate_upload_dirs():
        candidate = safe_join(directory, safe_name)
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


@documents_bp.route('/<path:filename>')
@login_required
def serve(filename):
    file_path = _find_document_path(filename)
    if not file_path:
        abort(404)

    as_attachment = request.args.get('download') == '1'
    return send_file(
        file_path,
        as_attachment=as_attachment,
        download_name=os.path.basename(file_path),
        conditional=True,
    )
