from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from app.extensions import mongo
from app.utils.helpers import now_utc
from app.utils.security import hash_password
from app.services.uid_service import generate_user_ref_id, generate_centre_uid, generate_mitra_uid
from app.services.validation_service import create_validation

ROLES = {
    'super_admin', 'avpl_admin', 'accounts', 'sales_nelocals', 'sales_unnatfarm', 'ufc_admin', 'ufc_mitra', 'farmer'
}
AUTO_APPROVED_ROLES = {'super_admin', 'avpl_admin', 'accounts', 'sales_nelocals', 'sales_unnatfarm'}


def _clean_optional_fields(doc):
    return {k: v for k, v in doc.items() if v is not None and not (isinstance(v, str) and v.strip() == '')}


def _build_user(name, role, username=None, phone=None, password='admin123', created_by=None, status='active', extra=None):
    extra = extra or {}
    user = {
        'user_ref_id': generate_user_ref_id(role),
        'name': name,
        'username': username,
        'phone': phone,
        'password_hash': hash_password(password),
        'role': role,
        'status': status,
        'active': True,
        'approval_status': 'approved' if role in AUTO_APPROVED_ROLES else 'pending_profile' if role in {'ufc_admin', 'ufc_mitra'} else 'pending',
        'created_by': created_by,
        'created_at': now_utc(),
        'updated_at': now_utc(),
        'last_login': None,
    }
    user.update({k: v for k, v in extra.items() if v not in [None, '']})
    if role == 'ufc_admin':
        user['centre_uid'] = generate_centre_uid(extra.get('state'))
    if role == 'ufc_mitra':
        centre_uid = extra.get('mapped_centre_uid')
        if not centre_uid:
            raise ValueError('Mapped UnnatFarm Centre UID is required for UFC Mitra creation.')
        user['mapped_centre_uid'] = centre_uid
        user['mitra_uid'] = generate_mitra_uid(centre_uid)
    return _clean_optional_fields(user)


def create_user(name, role, username=None, phone=None, password='admin123', created_by=None, status='active', extra=None):
    if role not in ROLES:
        raise ValueError('Invalid role.')
    if not name or not password:
        raise ValueError('Name and password are required.')
    if not username and not phone:
        raise ValueError('Username or phone is required.')
    if username and mongo.db.users.find_one({'username': username}):
        raise ValueError('This username already exists. Please use another username.')
    if phone and mongo.db.users.find_one({'phone': phone}):
        raise ValueError('This phone number already exists. Please use another phone number.')

    last_error = None
    for _ in range(1000):
        user = _build_user(name, role, username, phone, password, created_by, status, extra)
        try:
            result = mongo.db.users.insert_one(user)
            user['_id'] = result.inserted_id
            return user
        except DuplicateKeyError as exc:
            last_error = exc
            details = str(exc)
            if 'username' in details:
                raise ValueError('This username already exists. Please use another username.') from exc
            if 'phone' in details:
                raise ValueError('This phone number already exists. Please use another phone number.') from exc
            continue
    raise ValueError('Could not generate a unique system ID. Please try again.') from last_error


def get_user_for_login(identifier):
    return mongo.db.users.find_one({'$or': [{'username': identifier}, {'phone': identifier}]})


def update_last_login(user_id):
    mongo.db.users.update_one({'_id': ObjectId(user_id)}, {'$set': {'last_login': now_utc()}})


def create_farmer_registration(form):
    if mongo.db.users.find_one({'phone': form['contact_no']}):
        raise ValueError('Phone number already registered.')
    centre = mongo.db.ufc_admin_master.find_one({'centre_uid': form['centre_uid']}) or {}
    mitra = mongo.db.ufc_mitra_master.find_one({'mitra_uid': form['mitra_uid']}) or {}
    user = None
    last_error = None
    for _ in range(1000):
        candidate = {
            'user_ref_id': generate_user_ref_id('farmer'),
            'name': form['name'],
            'phone': form['contact_no'],
            'date_of_birth': form.get('date_of_birth'),
            'age': form.get('age'),
            'password_hash': hash_password(form['password']),
            'role': 'farmer',
            'status': 'active',
            'active': True,
            'approval_status': 'pending',
            'created_by': 'self',
            'created_at': now_utc(),
            'updated_at': now_utc(),
            'last_login': None,
            'mapped_centre_uid': form['centre_uid'],
            'mapped_mitra_uid': form['mitra_uid'],
            'state': form.get('state') or mitra.get('state') or centre.get('state'),
            'district': form.get('district') or mitra.get('district') or centre.get('district'),
            'block': form.get('block') or mitra.get('block') or centre.get('block'),
            'village': form.get('village') or mitra.get('village') or centre.get('village'),
        }
        candidate = _clean_optional_fields(candidate)
        try:
            result = mongo.db.users.insert_one(candidate)
            candidate['_id'] = result.inserted_id
            user = candidate
            break
        except DuplicateKeyError as exc:
            last_error = exc
            if 'phone' in str(exc):
                raise ValueError('Phone number already registered.') from exc
            continue
    if user is None:
        raise ValueError('Could not generate a unique farmer ID. Please try again.') from last_error

    master = {
        'linked_user_id': str(user['_id']),
        'name': form['name'],
        'gender': form['gender'],
        'date_of_birth': form.get('date_of_birth'),
        'age': form.get('age'),
        'contact_no': form['contact_no'],
        'centre_uid': form['centre_uid'],
        'mitra_uid': form['mitra_uid'],
        'state': user.get('state'),
        'district': user.get('district'),
        'block': user.get('block'),
        'village': form.get('village') or user.get('village'),
        'activities': form.get('activities', []),
        'agri_sub_categories': form.get('agri_sub_categories', []),
        'approval_status': 'pending',
        'created_at': now_utc(),
        'updated_at': now_utc(),
    }
    master = _clean_optional_fields(master)
    master_id = mongo.db.farmer_master.insert_one(master).inserted_id
    create_validation('farmer_registration', user['_id'], 'farmer', str(user['_id']), 'ufc_mitra', 'Farmer registration submitted.', metadata={'mapped_mitra_uid': form['mitra_uid'], 'mapped_centre_uid': form['centre_uid'], 'master_id': str(master_id)})
    return user


def complete_ufc_admin_profile(user_id, form):
    master = {
        'linked_user_id': str(user_id),
        'centre_uid': form['centre_uid'],
        'name_of_enterprise': form['name_of_enterprise'],
        'name_of_owner': form['name_of_owner'],
        'owner_dob': form.get('owner_dob'),
        'owner_age': form.get('owner_age'),
        'state': form.get('state'),
        'district': form.get('district'),
        'block': form.get('block'),
        'village': form.get('village'),
        'pan_number': form['pan_number'],
        'gst_number': form.get('gst_number') or '',
        'gst_registered': bool(form.get('gst_registered')),
        'trader_license_number': form.get('trader_license_number'),
        'other_licenses': form.get('other_licenses'),
        'approval_status': 'pending',
        'created_at': now_utc(),
        'updated_at': now_utc(),
    }
    master = _clean_optional_fields(master)
    existing = mongo.db.ufc_admin_master.find_one({'linked_user_id': str(user_id)})
    if existing:
        mongo.db.ufc_admin_master.update_one({'_id': existing['_id']}, {'$set': master})
        master_id = existing['_id']
    else:
        master_id = mongo.db.ufc_admin_master.insert_one(master).inserted_id
    mongo.db.users.update_one(
    {'_id': ObjectId(user_id)},
    {
        '$set': {
            'approval_status': 'pending',
            **{
                k: master[k]
                for k in [
                    'centre_uid',
                    'state',
                    'district',
                    'block',
                    'village',
                    'owner_dob',
                    'owner_age'
                ]
                if k in master
            }
        }
    }
)
    create_validation('ufc_admin_profile', user_id, 'ufc_admin', str(user_id), 'avpl_admin', 'UFC Admin profile submitted.', metadata={'master_id': str(master_id)})
    return str(master_id)


def complete_ufc_mitra_profile(user_id, form):
    centre = mongo.db.ufc_admin_master.find_one({'centre_uid': form['mapped_centre_uid']}) or mongo.db.users.find_one({'centre_uid': form['mapped_centre_uid']}) or {}
    master = {
        'linked_user_id': str(user_id),
        'mitra_uid': form['mitra_uid'],
        'mapped_centre_uid': form['mapped_centre_uid'],
        'name': form['name'],
        'care_of': form['care_of'],
        'dob': form['dob'],
        'age': form['age'],
        'education': form['education'],
        'gender': form['gender'],
        'government_id_number': form['government_id_number'],
        'state': form.get('state') or centre.get('state'),
        'district': form.get('district') or centre.get('district'),
        'block': form.get('block') or centre.get('block'),
        'village': form.get('village') or centre.get('village'),
        'approval_status': 'pending',
        'created_at': now_utc(),
        'updated_at': now_utc(),
    }
    master = _clean_optional_fields(master)
    existing = mongo.db.ufc_mitra_master.find_one({'linked_user_id': str(user_id)})
    if existing:
        mongo.db.ufc_mitra_master.update_one({'_id': existing['_id']}, {'$set': master})
        master_id = existing['_id']
    else:
        master_id = mongo.db.ufc_mitra_master.insert_one(master).inserted_id
    mongo.db.users.update_one(
    {'_id': ObjectId(user_id)},
    {
        '$set': {
            'approval_status': 'pending',
            **{
                k: master[k]
                for k in [
                    'mitra_uid',
                    'mapped_centre_uid',
                    'state',
                    'district',
                    'block',
                    'village',
                    'dob',
                    'age'
                ]
                if k in master
            }
        }
    }
)
    create_validation('ufc_mitra_profile', user_id, 'ufc_mitra', str(user_id), 'avpl_admin', 'UFC Mitra profile submitted.', metadata={'master_id': str(master_id)})
    return str(master_id)
