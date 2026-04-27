from pymongo import ReturnDocument
from app.extensions import mongo
from app.services.location_service import get_state_code

ROLE_PREFIXES = {
    'super_admin': 'SUP', 'avpl_admin': 'AVP', 'accounts': 'ACC', 'sales_nelocals': 'SNL',
    'sales_unnatfarm': 'SUF', 'ufc_admin': 'UFA', 'ufc_mitra': 'UFM', 'farmer': 'FAR',
}


def _next_seq(name):
    row = mongo.db.counters.find_one_and_update(
        {'_id': name}, {'$inc': {'value': 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return int(row.get('value', 1))


def generate_user_ref_id(role):
    prefix = ROLE_PREFIXES.get(role, role[:3].upper())
    seq = _next_seq(f'user_ref_{role}')
    return f'USR-{prefix}-{seq:05d}'


def generate_centre_uid(state):
    code = get_state_code(state) or 'UF'
    seq = _next_seq(f'centre_uid_{code}')
    return f'{code}{seq:04d}'


def generate_mitra_uid(centre_uid):
    if not centre_uid:
        raise ValueError('Centre UID is required to generate Mitra UID.')
    seq = _next_seq(f'mitra_uid_{centre_uid}')
    return f'{centre_uid}-{seq:02d}'
