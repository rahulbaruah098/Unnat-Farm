import os
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import current_app


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password, password_hash):
    return check_password_hash(password_hash, password)


def ensure_upload_folder(app):
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def is_allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in current_app.config["ALLOWED_EXTENSIONS"]


def save_file(file_storage, prefix="doc"):
    if not file_storage or file_storage.filename == "":
        return None
    if not is_allowed_file(file_storage.filename):
        raise ValueError("Invalid file type.")
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    fname = secure_filename(f"{prefix}_{secrets.token_hex(8)}.{ext}")
    file_storage.save(os.path.join(current_app.config["UPLOAD_FOLDER"], fname))
    return fname
