from flask import session


def set_user_session(user):
    session["user_id"] = str(user["_id"])
    session["name"] = user.get("name")
    session["role"] = user.get("role")
    session["approval_status"] = user.get("approval_status")
    session["centre_uid"] = user.get("centre_uid")
    session["mitra_uid"] = user.get("mitra_uid")
    session["mapped_centre_uid"] = user.get("mapped_centre_uid")
    session["mapped_mitra_uid"] = user.get("mapped_mitra_uid")


def clear_user_session():
    session.clear()
