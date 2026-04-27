from app.extensions import mongo

# Fixed dropdown only for Indian states/UTs. District, block, and village are
# manual fields because complete master data is not available yet. The system
# learns typed values from records for future dropdown/reporting use.
STATE_CODES = {
    'Andhra Pradesh': 'AP',
    'Arunachal Pradesh': 'AR',
    'Assam': 'AS',
    'Bihar': 'BR',
    'Chhattisgarh': 'CG',
    'Goa': 'GA',
    'Gujarat': 'GJ',
    'Haryana': 'HR',
    'Himachal Pradesh': 'HP',
    'Jharkhand': 'JH',
    'Karnataka': 'KA',
    'Kerala': 'KL',
    'Madhya Pradesh': 'MP',
    'Maharashtra': 'MH',
    'Manipur': 'MN',
    'Meghalaya': 'ML',
    'Mizoram': 'MZ',
    'Nagaland': 'NL',
    'Odisha': 'OD',
    'Punjab': 'PB',
    'Rajasthan': 'RJ',
    'Sikkim': 'SK',
    'Tamil Nadu': 'TN',
    'Telangana': 'TS',
    'Tripura': 'TR',
    'Uttar Pradesh': 'UP',
    'Uttarakhand': 'UK',
    'West Bengal': 'WB',
    'Andaman and Nicobar Islands': 'AN',
    'Chandigarh': 'CH',
    'Dadra and Nagar Haveli and Daman and Diu': 'DN',
    'Delhi': 'DL',
    'Jammu and Kashmir': 'JK',
    'Ladakh': 'LA',
    'Lakshadweep': 'LD',
    'Puducherry': 'PY',
}


def seed_locations(force=False):
    if force:
        mongo.db.location_master.delete_many({})
    if mongo.db.location_master.estimated_document_count() > 0:
        return
    mongo.db.location_master.insert_many([
        {'state': state, 'state_code': code} for state, code in STATE_CODES.items()
    ])


def get_state_code(state):
    if not state:
        return ''
    found = mongo.db.location_master.find_one({'state': state}, {'state_code': 1})
    if found:
        return found.get('state_code', '')
    return STATE_CODES.get(state, state[:2].upper())


def list_states():
    seed_locations()
    states = list(mongo.db.location_master.distinct('state'))
    return sorted(states or list(STATE_CODES.keys()))


def _learned_values(field, query):
    values = set()
    for collection in [mongo.db.ufc_admin_master, mongo.db.ufc_mitra_master, mongo.db.farmer_master, mongo.db.users]:
        try:
            for value in collection.distinct(field, query):
                if isinstance(value, str) and value.strip():
                    values.add(value.strip())
        except Exception:
            pass
    return sorted(values)


def list_districts(state):
    return _learned_values('district', {'state': state}) if state else []


def list_blocks(state, district=None):
    query = {'state': state} if state else {}
    if district:
        query['district'] = district
    return _learned_values('block', query)


def list_villages(state, district=None, block=None):
    query = {'state': state} if state else {}
    if district:
        query['district'] = district
    if block:
        query['block'] = block
    return _learned_values('village', query)
