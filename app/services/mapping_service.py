from app.extensions import mongo


def validate_mitra_under_centre(centre_uid, mitra_uid):
    mitra = mongo.db.ufc_mitra_master.find_one({"mitra_uid": mitra_uid})
    if not mitra:
        return False, "Invalid UFC Mitra UID."
    if mitra.get("mapped_centre_uid") != centre_uid:
        return False, "UFC Mitra does not belong to the provided UFC Admin centre."
    return True, "Valid mapping."


def validate_farmer_mapping(centre_uid, mitra_uid):
    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid})
    if not centre:
        return False, "Invalid UnnatFarm Centre UID."
    return validate_mitra_under_centre(centre_uid, mitra_uid)
