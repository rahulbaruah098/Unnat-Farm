from bson import ObjectId
from datetime import datetime
import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from app.extensions import mongo
from app.utils.security import verify_password
from app.utils.session import set_user_session, clear_user_session
from app.utils.decorators import login_required
from app.services.user_service import get_user_for_login, update_last_login, create_farmer_registration, complete_ufc_admin_profile, complete_ufc_mitra_profile
from app.services.mapping_service import validate_farmer_mapping
from app.services.document_service import store_document
from app.services.location_service import list_states, list_districts, list_blocks, list_villages

auth_bp = Blueprint('auth', __name__)


def _latest_validation_for_user(user_id, entity_type=None):
    query = {'entity_id': str(user_id)}
    if entity_type:
        query['entity_type'] = entity_type
    return mongo.db.validations.find_one(query, sort=[('updated_at', -1), ('created_at', -1)])

@auth_bp.route("/")
def login_select():
    return render_template("auth/login_select.html")

#changes  by atlanta
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    is_json = request.is_json or request.headers.get('Content-Type') == 'application/json'

    # ---------- APP LOGIN (UPDATED FOR ALL APPS) ----------
    if is_json:
        data = request.get_json(silent=True) or {}

        identifier = (data.get('identifier') or '').strip()
        password = (data.get('password') or '').strip()

        if not identifier or not password:
            return jsonify({'ok': False, 'message': 'Username and password required'}), 400

        user = get_user_for_login(identifier)

        if not user or not verify_password(password, user.get('password_hash', '')):
            return jsonify({'ok': False, 'message': 'Invalid credentials'}), 401

        if not user.get('active', True):
            return jsonify({'ok': False, 'message': 'Account inactive'}), 403

        role = (user.get('role') or '').strip().lower()

        # ✅ ALLOW ALL APP ROLES
        allowed_roles = ['ufc_admin', 'ufc_mitra', 'farmer']

        if role not in allowed_roles:
            return jsonify({'ok': False, 'message': 'Role not allowed in mobile app'}), 403

        update_last_login(str(user['_id']))
        set_user_session(user)

        # Get latest validation depending on role
        entity_map = {
            'ufc_admin': 'ufc_admin_profile',
            'ufc_mitra': 'ufc_mitra_profile',
            'farmer': 'farmer'
        }

        latest_validation = _latest_validation_for_user(
            str(user['_id']),
            entity_map.get(role)
        ) or {}

        return jsonify({
            'ok': True,
            'message': 'Login successful',
            'user': {
                'id': str(user['_id']),
                'username': user.get('username') or user.get('name') or identifier,
                'role': role,

                # UID fields
                'centre_uid': user.get('centre_uid') or '',
                'mitra_uid': user.get('mitra_uid') or '',
                'mapped_centre_uid': user.get('mapped_centre_uid') or '',

                # approval flow
                'approval_status': user.get('approval_status') or 'pending_profile',
                'rejection_reason': (
                    user.get('latest_rejection_reason')
                    or latest_validation.get('rejection_reason')
                    or latest_validation.get('action_remarks')
                    or ''
                ),

                # location
                'state': user.get('state') or '',
                'district': user.get('district') or '',
                'block': user.get('block') or '',
                'village': user.get('village') or '',
            }
        }), 200

    # ---------- WEB LOGIN (EXISTING) ----------
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '').strip()
        login_type = request.form.get('login_type', '').strip()

        user = get_user_for_login(identifier)

        if not user or not verify_password(password, user.get('password_hash', '')):
            flash('Invalid credentials.', 'danger')

            role_routes = {
            'authority': 'auth.login_authority',
            'centre': 'auth.login_centre',
            'mitra': 'auth.login_mitra',
            'farmer': 'auth.login_farmer',
            }

            return redirect(url_for(role_routes.get(login_type, 'auth.login_select')))

        if not user.get('active', True):
            flash('Account is inactive.', 'danger')

            role_routes = {
            'authority': 'auth.login_authority',
            'centre': 'auth.login_centre',
            'mitra': 'auth.login_mitra',
            'farmer': 'auth.login_farmer',
            }

            return redirect(url_for(role_routes.get(login_type, 'auth.login_select')))

        user_role = user.get('role', '').strip()

        allowed_roles = {
            'authority': [
                'super_admin',
                'avpl_admin',
                'accounts',
                'sales_unnatfarm',
                'sales_nelocals'
            ],
            'centre': [
                'ufc_admin'
            ],
            'mitra': [
                'ufc_mitra'
            ],
            'farmer': [
                'farmer'
            ]
        }

        if login_type:
            if user_role not in allowed_roles.get(login_type, []):
                flash('You are not allowed to login from this login type.', 'danger')

                role_routes = {
                'authority': 'auth.login_authority',
                'centre': 'auth.login_centre',
                'mitra': 'auth.login_mitra',
                'farmer': 'auth.login_farmer',
                }

                return redirect(url_for(role_routes.get(login_type, 'auth.login_select')))

            update_last_login(str(user['_id']))
        set_user_session(user)

        role_login_routes = {
            'authority': 'auth.login_authority',
            'centre': 'auth.login_centre',
            'mitra': 'auth.login_mitra',
            'farmer': 'auth.login_farmer',
        }

        if login_type in role_login_routes:
            session['last_login_page'] = role_login_routes[login_type]
        else:
            session['last_login_page'] = 'auth.login_select'

        return redirect(url_for('dashboard.home'))

    return redirect(url_for('auth.login_select'))

@auth_bp.route('/login/authority')
def login_authority():
    return render_template(
        'auth/role_login.html',
        role_title='Authority Login',
        role_subtitle='Secure access for admins and management users.',
        role_key='authority'
    )


@auth_bp.route('/login/centre')
def login_centre():
    return render_template(
        'auth/role_login.html',
        role_title='UnnatFarm Centre Login',
        role_subtitle='Access centre operations, services and dashboard.',
        role_key='centre'
    )


@auth_bp.route('/login/mitra')
def login_mitra():
    return render_template(
        'auth/role_login.html',
        role_title='UnnatFarm Mitra Login',
        role_subtitle='Access field support and farmer service tools.',
        role_key='mitra'
    )


@auth_bp.route('/login/farmer')
def login_farmer():
    return render_template(
        'auth/role_login.html',
        role_title='Farmer Login',
        role_subtitle='Access farmer account, services and support.',
        role_key='farmer'
    )


@auth_bp.route('/logout')
def logout():
    last_login_page = session.get('last_login_page', 'auth.login_select')

    clear_user_session()

    flash('Logged out successfully.', 'success')
    return redirect(url_for(last_login_page))


@auth_bp.route("/privacy-policy")
def privacy_policy():
    return render_template("auth/privacy_policy.html")

#Changes by atlanta
@auth_bp.route('/register/farmer', methods=['GET', 'POST'])
def register_farmer():
    states = list_states()

    is_json = request.is_json or request.headers.get('Content-Type') == 'application/json'
    is_app_multipart = request.form.get('app') == '1'

    def _as_list(value):
        if value is None:
            return []

        if isinstance(value, list):
            return value

        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []

            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass

            return [value]

        return []

    def _validate_profile_photo(file):
        if not file or not file.filename:
            return True, ""

        allowed_image_types = {
            'image/jpeg',
            'image/png',
            'image/jpg',
            'image/webp'
        }

        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        file_ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        allowed_extensions = {"jpg", "jpeg", "png", "webp"}

        if file.content_type not in allowed_image_types and file_ext not in allowed_extensions:
            return False, 'Only JPG, PNG or WEBP image files are allowed for farmer profile photo.'

        if file_size > 2 * 1024 * 1024:
            return False, 'Farmer profile photo must be less than or equal to 2 MB.'

        return True, ""

    def _save_initial_profile_photo(profile_photo_file, farmer_user, farmer_user_id):
        if not profile_photo_file or not profile_photo_file.filename:
            return None

        farmer_master = mongo.db.farmer_master.find_one({
            'linked_user_id': farmer_user_id
        }) or {}

        doc = store_document(
        profile_photo_file,
        farmer_user_id,
        farmer_master.get('_id'),
        farmer_user_id,
        'farmer',
        'Passport Size Photo'
        )

        profile_photo_path = (
            doc.get('file_path')
            or doc.get('filename')
            or doc.get('file_name')
        ) if doc else None

        if profile_photo_path:
            mongo.db.farmer_master.update_one(
                {
                    '$or': [
                        {'linked_user_id': farmer_user_id},
                        {'linked_user_id': farmer_user['_id']},
                        {'contact_no': farmer_user.get('phone')},
                    ]
                },
                {
                    '$set': {
                        'profile_photo': profile_photo_path,
                        'profile_photo_file': profile_photo_path,
                        'updated_at': datetime.utcnow()
                    }
                }
            )

            mongo.db.users.update_one(
                {'_id': farmer_user['_id']},
                {
                    '$set': {
                        'profile_photo': profile_photo_path,
                        'profile_photo_file': profile_photo_path,
                        'updated_at': datetime.utcnow()
                    }
                }
            )

        return profile_photo_path

    # ---------- APP JSON / APP MULTIPART REGISTRATION ----------
    if is_json or is_app_multipart:
        if is_json:
            data = request.get_json(silent=True) or {}

            form = {
        'name': (data.get('name') or '').strip(),
        'gender': (data.get('gender') or '').strip(),
        'date_of_birth': (data.get('date_of_birth') or data.get('dob') or '').strip(),
        'age': str(data.get('age') or '').strip(),
        'contact_no': (data.get('contact_no') or '').strip(),
        'password': (data.get('password') or '').strip(),
        'centre_uid': (data.get('centre_uid') or '').strip(),
        'mitra_uid': (data.get('mitra_uid') or '').strip(),
        'state': (data.get('state') or '').strip(),
        'district': (data.get('district') or '').strip(),
        'block': (data.get('block') or '').strip(),
        'village': (data.get('village') or '').strip(),
        'activities': data.get('activities') or [],
        'agri_sub_categories': data.get('agri_sub_categories') or [],
    }

            profile_photo_file = None

        else:
            form = {
    'name': request.form.get('name', '').strip(),
    'gender': request.form.get('gender', '').strip(),
    'date_of_birth': (
        request.form.get('date_of_birth', '').strip()
        or request.form.get('dob', '').strip()
    ),
    'age': request.form.get('age', '').strip(),
    'contact_no': request.form.get('contact_no', '').strip(),
    'password': request.form.get('password', '').strip(),
    'centre_uid': request.form.get('centre_uid', '').strip(),
    'mitra_uid': request.form.get('mitra_uid', '').strip(),
    'state': request.form.get('state', '').strip(),
    'district': request.form.get('district', '').strip(),
    'block': request.form.get('block', '').strip(),
    'village': request.form.get('village', '').strip(),
    'activities': _as_list(request.form.get('activities')),
    'agri_sub_categories': _as_list(request.form.get('agri_sub_categories')),
}

            profile_photo_file = request.files.get('profile_photo')

        required_fields = [
    'name',
    'gender',
    'date_of_birth',
    'age',
    'contact_no',
    'password',
    'centre_uid',
    'mitra_uid',
    'village',
]

        missing = [field for field in required_fields if not form.get(field)]

        if missing:
            return jsonify({
                'ok': False,
                'message': 'Please fill all required fields.',
                'missing_fields': missing
            }), 400

        if not isinstance(form['activities'], list):
            form['activities'] = []

        if not isinstance(form['agri_sub_categories'], list):
            form['agri_sub_categories'] = []

        valid, message = validate_farmer_mapping(
            form['centre_uid'],
            form['mitra_uid']
        )

        if not valid:
            return jsonify({
                'ok': False,
                'message': message
            }), 400

        if mongo.db.users.find_one({'phone': form['contact_no']}):
            return jsonify({
                'ok': False,
                'message': 'Phone number already registered.'
            }), 409

        photo_ok, photo_message = _validate_profile_photo(profile_photo_file)
        if not photo_ok:
            return jsonify({
                'ok': False,
                'message': photo_message
            }), 400

        try:
            farmer_user = create_farmer_registration(form)
        except ValueError as exc:
            return jsonify({
                'ok': False,
                'message': str(exc)
            }), 400

        farmer_user_id = str(farmer_user['_id'])
        profile_photo_path = _save_initial_profile_photo(
            profile_photo_file,
            farmer_user,
            farmer_user_id
        )

        return jsonify({
            'ok': True,
            'message': 'Farmer registration submitted. Wait for UFC Mitra validation.',
            'approval_status': 'pending',
            'profile_photo': profile_photo_path or ''
        }), 201

    # ---------- WEB FARMER REGISTRATION ----------
    if request.method == 'POST':
        form = {
    'name': request.form.get('name', '').strip(),
    'gender': request.form.get('gender', '').strip(),
    'date_of_birth': request.form.get('date_of_birth', '').strip(),
    'age': request.form.get('age', '').strip(),
    'contact_no': request.form.get('contact_no', '').strip(),
    'password': request.form.get('password', '').strip(),
    'centre_uid': request.form.get('centre_uid', '').strip(),
    'mitra_uid': request.form.get('mitra_uid', '').strip(),
    'state': request.form.get('state', '').strip(),
    'district': request.form.get('district', '').strip(),
    'block': request.form.get('block', '').strip(),
    'village': request.form.get('village', '').strip(),
    'activities': request.form.getlist('activities'),
    'agri_sub_categories': request.form.getlist('agri_sub_categories'),
}

        if not form['centre_uid'] or not form['mitra_uid']:
            flash('Centre UID and UFC Mitra UID are mandatory for farmer registration.', 'danger')
            return render_template('auth/register_farmer.html', data=form, states=states)

        valid, message = validate_farmer_mapping(form['centre_uid'], form['mitra_uid'])

        if not valid:
            flash(message, 'danger')
            return render_template('auth/register_farmer.html', data=form, states=states)

        if mongo.db.users.find_one({'phone': form['contact_no']}):
            flash('Phone number already registered.', 'danger')
            return render_template('auth/register_farmer.html', data=form, states=states)

        profile_photo_file = request.files.get('profile_photo')

        photo_ok, photo_message = _validate_profile_photo(profile_photo_file)
        if not photo_ok:
            flash(photo_message, 'danger')
            return render_template('auth/register_farmer.html', data=form, states=states)

        try:
            farmer_user = create_farmer_registration(form)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template('auth/register_farmer.html', data=form, states=states)

        farmer_user_id = str(farmer_user['_id'])
        _save_initial_profile_photo(profile_photo_file, farmer_user, farmer_user_id)

        flash('Farmer registration submitted. Wait for UFC Mitra validation.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/register_farmer.html', states=states)


@auth_bp.route('/profile/farmer/complete', methods=['GET', 'POST'])
@login_required
def complete_farmer():
    if session.get('role') != 'farmer':
        return redirect(url_for('dashboard.home'))

    states = list_states()
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})

    if not user:
        flash('User not found. Please login again.', 'danger')
        return redirect(url_for('auth.logout'))

    farmer_master = (
        mongo.db.farmer_master.find_one({'linked_user_id': session['user_id']})
        or mongo.db.farmer_master.find_one({'linked_user_id': ObjectId(session['user_id'])})
        or mongo.db.farmer_master.find_one({'contact_no': user.get('phone')})
        or {}
    )

    latest_validation = _latest_validation_for_user(session['user_id'], 'farmer_registration') or {}

    rejection_reason = (
        user.get('latest_rejection_reason')
        or latest_validation.get('rejection_reason')
        or latest_validation.get('action_remarks')
        or ''
    )

    data = {
        'name': farmer_master.get('name') or user.get('name') or '',
        'gender': farmer_master.get('gender') or user.get('gender') or '',
        'age': farmer_master.get('age') or user.get('age') or '',
        'contact_no': farmer_master.get('contact_no') or user.get('phone') or '',
        'centre_uid': farmer_master.get('centre_uid') or user.get('mapped_centre_uid') or '',
        'mitra_uid': farmer_master.get('mitra_uid') or user.get('mapped_mitra_uid') or '',
        'state': farmer_master.get('state') or user.get('state') or '',
        'district': farmer_master.get('district') or user.get('district') or '',
        'block': farmer_master.get('block') or user.get('block') or '',
        'village': farmer_master.get('village') or user.get('village') or '',
        'activities': farmer_master.get('activities') or [],
        'agri_sub_categories': farmer_master.get('agri_sub_categories') or [],
    }

    if request.method == 'POST':
        form = {
            'name': request.form.get('name', '').strip(),
            'gender': request.form.get('gender', '').strip(),
            'age': request.form.get('age', '').strip(),
            'contact_no': user.get('phone') or request.form.get('contact_no', '').strip(),
            'centre_uid': request.form.get('centre_uid', '').strip(),
            'mitra_uid': request.form.get('mitra_uid', '').strip(),
            'state': request.form.get('state', '').strip(),
            'district': request.form.get('district', '').strip(),
            'block': request.form.get('block', '').strip(),
            'village': request.form.get('village', '').strip(),
            'activities': request.form.getlist('activities'),
            'agri_sub_categories': request.form.getlist('agri_sub_categories'),
        }

        if not form['centre_uid'] or not form['mitra_uid']:
            flash('Centre UID and UFC Mitra UID are mandatory.', 'danger')
            return render_template(
                'auth/register_farmer.html',
                data=form,
                states=states,
                rejection_reason=rejection_reason,
                correction_mode=True
            )

        valid, message = validate_farmer_mapping(form['centre_uid'], form['mitra_uid'])

        if not valid:
            flash(message, 'danger')
            return render_template(
                'auth/register_farmer.html',
                data=form,
                states=states,
                rejection_reason=rejection_reason,
                correction_mode=True
            )

        update_payload = {
            'name': form['name'],
            'gender': form['gender'],
            'age': form['age'],
            'contact_no': form['contact_no'],
            'centre_uid': form['centre_uid'],
            'mitra_uid': form['mitra_uid'],
            'state': form['state'],
            'district': form['district'],
            'block': form['block'],
            'village': form['village'],
            'activities': form['activities'],
            'agri_sub_categories': form['agri_sub_categories'],
            'approval_status': 'pending',
            'latest_rejection_reason': '',
            'updated_at': datetime.utcnow()
        }

        mongo.db.farmer_master.update_one(
            {
                '$or': [
                    {'linked_user_id': session['user_id']},
                    {'linked_user_id': ObjectId(session['user_id'])},
                    {'contact_no': user.get('phone')}
                ]
            },
            {'$set': update_payload},
            upsert=False
        )

        mongo.db.users.update_one(
            {'_id': ObjectId(session['user_id'])},
            {
                '$set': {
                    'name': form['name'],
                    'phone': form['contact_no'],
                    'mapped_centre_uid': form['centre_uid'],
                    'mapped_mitra_uid': form['mitra_uid'],
                    'state': form['state'],
                    'district': form['district'],
                    'block': form['block'],
                    'village': form['village'],
                    'approval_status': 'pending',
                    'latest_rejection_reason': '',
                    'updated_at': datetime.utcnow()
                }
            }
        )

        profile_photo_file = request.files.get('profile_photo')

        if profile_photo_file and profile_photo_file.filename:
            doc = store_document(
                profile_photo_file,
                session['user_id'],
                farmer_master.get('_id'),
                session['user_id'],
                'farmer',
                'Passport Size Photo'
            )

            profile_photo_path = (
                doc.get('file_path')
                or doc.get('filename')
                or doc.get('file_name')
            ) if doc else None

            if profile_photo_path:
                mongo.db.farmer_master.update_one(
                    {'linked_user_id': session['user_id']},
                    {
                        '$set': {
                            'profile_photo': profile_photo_path,
                            'updated_at': datetime.utcnow()
                        }
                    }
                )

                mongo.db.users.update_one(
                    {'_id': ObjectId(session['user_id'])},
                    {
                        '$set': {
                            'profile_photo': profile_photo_path,
                            'updated_at': datetime.utcnow()
                        }
                    }
                )

        mongo.db.validations.update_one(
            {
                'entity_id': session['user_id'],
                'entity_type': 'farmer_registration'
            },
            {
                '$set': {
                    'status': 'pending',
                    'approver_role': 'ufc_mitra',
                    'target_role': 'farmer',
                    'rejection_reason': '',
                    'action_remarks': '',
                    'remarks': '',
                    'metadata': {
                        'mapped_mitra_uid': form['mitra_uid'],
                        'centre_uid': form['centre_uid']
                    },
                    'updated_at': datetime.utcnow()
                },
                '$setOnInsert': {
                    'created_by_user_id': session['user_id'],
                    'created_at': datetime.utcnow()
                }
            },
            upsert=True
        )

        session['approval_status'] = 'pending'
        session.pop('rejection_reason', None)

        flash('Farmer profile resubmitted for validation.', 'success')
        return redirect(url_for('dashboard.pending_access'))

    return render_template(
        'auth/register_farmer.html',
        data=data,
        states=states,
        rejection_reason=rejection_reason,
        correction_mode=True
    )



from datetime import date, datetime

def calculate_age_from_dob(dob_value):
    if not dob_value:
        return ""

    try:
        dob = datetime.strptime(str(dob_value), "%Y-%m-%d").date()
        today = date.today()

        if dob > today:
            return ""

        age = today.year - dob.year - (
            (today.month, today.day) < (dob.month, dob.day)
        )

        return str(age) if age >= 0 else ""
    except Exception:
        return ""

#changes by atlanta
@auth_bp.route('/profile/ufc-admin/complete', methods=['GET', 'POST'])
def complete_ufc_admin():
    is_json = request.is_json or request.headers.get('Content-Type') == 'application/json'
    is_app = (
        is_json
        or request.form.get('app') == '1'
        or request.args.get('app') == '1'
        or request.headers.get('Accept') == 'application/json'
    )

    def _doc_value(doc):
        if not doc:
            return ''

        return (
            doc.get('file_path')
            or doc.get('filename')
            or doc.get('file_name')
            or doc.get('stored_name')
            or doc.get('path')
            or ''
        )

    def _doc_name(doc):
        if not doc:
            return ''

        return (
            doc.get('original_filename')
            or doc.get('original_name')
            or doc.get('display_name')
            or doc.get('filename')
            or doc.get('file_name')
            or doc.get('stored_name')
            or ''
        )

    def _latest_ufc_admin_doc(user_id, master_id, label):
        owner_terms = [
            {'user_id': user_id},
            {'owner_user_id': user_id},
            {'linked_user_id': user_id},
            {'created_by_user_id': user_id},
            {'uploaded_by': user_id},
            {'entity_id': user_id},
            {'entity_user_id': user_id},
        ]

        try:
            user_obj_id = ObjectId(user_id)
            owner_terms.extend([
                {'user_id': user_obj_id},
                {'owner_user_id': user_obj_id},
                {'linked_user_id': user_obj_id},
                {'created_by_user_id': user_obj_id},
                {'uploaded_by': user_obj_id},
                {'entity_id': user_obj_id},
                {'entity_user_id': user_obj_id},
            ])
        except Exception:
            pass

        if master_id:
            owner_terms.extend([
                {'master_id': master_id},
                {'record_id': master_id},
                {'entity_master_id': master_id},
                {'parent_id': master_id},
                {'entity_id': master_id},
                {'master_id': str(master_id)},
                {'record_id': str(master_id)},
                {'entity_master_id': str(master_id)},
                {'parent_id': str(master_id)},
                {'entity_id': str(master_id)},
            ])

        label_terms = [
            {'label': label},
            {'document_label': label},
            {'document_type': label},
            {'doc_type': label},
            {'type': label},
            {'title': label},
            {'category': label},
        ]

        return mongo.db.documents.find_one(
            {
                '$and': [
                    {'$or': owner_terms},
                    {'$or': label_terms},
                ]
            },
            sort=[('updated_at', -1), ('created_at', -1), ('_id', -1)]
        )

    def _existing_ufc_admin_documents(user_id, master):
        master_id = master.get('_id') if master else None

        doc_map = {
            'registration_certificate': 'Registration Certificate',
            'pan_file': 'PAN',
            'gst_file': 'GST',
            'trader_license_file': 'Trader License',
            'other_license_file': 'Other Licenses',
        }

        result = {}

        for field, label in doc_map.items():
            doc = _latest_ufc_admin_doc(user_id, master_id, label)

            result[field] = {
                'label': label,
                'name': _doc_name(doc),
                'path': _doc_value(doc),
                'url': _doc_value(doc),
                'exists': bool(_doc_value(doc)),
            }

        return result

    if is_app:
        user_id = (
            request.args.get('user_id')
            or request.form.get('user_id')
            or ((request.get_json(silent=True) or {}).get('user_id') if is_json else '')
            or ''
        ).strip()

        if not user_id:
            return jsonify({'ok': False, 'message': 'User ID is required.'}), 400

        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            return jsonify({'ok': False, 'message': 'Invalid user ID.'}), 400

        user = mongo.db.users.find_one({'_id': user_obj_id})

        if not user:
            return jsonify({'ok': False, 'message': 'User not found.'}), 404

        if (user.get('role') or '').strip().lower() != 'ufc_admin':
            return jsonify({'ok': False, 'message': 'Invalid user role.'}), 403

        master = (
            mongo.db.ufc_admin_master.find_one({'linked_user_id': user_id})
                or mongo.db.ufc_admin_master.find_one({'linked_user_id': user_obj_id})
                or mongo.db.ufc_admin_master.find_one({'centre_uid': user.get('centre_uid')})
            or {}
        )

        latest_validation = _latest_validation_for_user(user_id, 'ufc_admin_profile') or {}

        rejection_reason = (
            user.get('latest_rejection_reason')
            or latest_validation.get('rejection_reason')
            or latest_validation.get('action_remarks')
            or ''
        )

        if request.method == 'GET':
            profile_payload = {
                'centre_uid': user.get('centre_uid') or master.get('centre_uid') or '',
                'name_of_enterprise': master.get('name_of_enterprise') or '',
                'name_of_owner': master.get('name_of_owner') or '',
                'owner_dob': master.get('owner_dob') or '',
                'owner_age': master.get('owner_age') or '',
                'state': master.get('state') or user.get('state') or '',
                'district': master.get('district') or user.get('district') or '',
                'block': master.get('block') or user.get('block') or '',
                'village': master.get('village') or user.get('village') or '',
                'pan_number': master.get('pan_number') or '',
                'gst_number': master.get('gst_number') or '',
                'trader_license_number': master.get('trader_license_number') or '',
                'other_licenses': master.get('other_licenses') or '',
                'approval_status': user.get('approval_status') or 'pending_profile',
                'rejection_reason': rejection_reason,
            }

            return jsonify({
                'ok': True,
                'message': 'UFC Admin profile loaded.',
                'profile': profile_payload,
                'master': profile_payload,
                'existing_documents': _existing_ufc_admin_documents(user_id, master),
                'latest_validation': {
                    'status': latest_validation.get('status', ''),
                    'rejection_reason': rejection_reason,
                    'action_remarks': latest_validation.get('action_remarks', ''),
                }
            }), 200

        if is_json:
            data = request.get_json(silent=True) or {}

            owner_dob = (data.get('owner_dob') or '').strip()

            form = {
                'centre_uid': user.get('centre_uid'),
                'name_of_enterprise': (data.get('name_of_enterprise') or '').strip(),
                'name_of_owner': (data.get('name_of_owner') or '').strip(),
                'owner_dob': owner_dob,
                'owner_age': calculate_age_from_dob(owner_dob),
                'state': (data.get('state') or user.get('state') or '').strip(),
                'district': (data.get('district') or user.get('district') or '').strip(),
                'block': (data.get('block') or user.get('block') or '').strip(),
                'village': (data.get('village') or user.get('village') or '').strip(),
                'pan_number': (data.get('pan_number') or '').strip(),
                'gst_number': (data.get('gst_number') or '').strip(),
                'trader_license_number': (data.get('trader_license_number') or '').strip(),
                'other_licenses': (data.get('other_licenses') or '').strip(),
            }
        else:
            owner_dob = request.form.get('owner_dob', '').strip()

            form = {
                'centre_uid': user.get('centre_uid'),
                'name_of_enterprise': request.form.get('name_of_enterprise', '').strip(),
                'name_of_owner': request.form.get('name_of_owner', '').strip(),
                'owner_dob': owner_dob,
                'owner_age': calculate_age_from_dob(owner_dob),
                'state': request.form.get('state', '').strip() or user.get('state', ''),
                'district': request.form.get('district', '').strip() or user.get('district', ''),
                'block': request.form.get('block', '').strip() or user.get('block', ''),
                'village': request.form.get('village', '').strip() or user.get('village', ''),
                'pan_number': request.form.get('pan_number', '').strip(),
                'gst_number': request.form.get('gst_number', '').strip(),
                'trader_license_number': request.form.get('trader_license_number', '').strip(),
                'other_licenses': request.form.get('other_licenses', '').strip(),
            }

        if not form['name_of_enterprise'] or not form['name_of_owner']:
            return jsonify({
                'ok': False,
                'message': 'Enterprise name and owner name are required.'
            }), 400

        if not form['owner_dob'] or not form['owner_age']:
            return jsonify({
                'ok': False,
                'message': 'Please enter a valid Owner Date of Birth.'
            }), 400

        master_id = complete_ufc_admin_profile(user_id, form)

        if not is_json:
            doc_map = {
                'registration_certificate': 'Registration Certificate',
                'pan_file': 'PAN',
                'gst_file': 'GST',
                'trader_license_file': 'Trader License',
                'other_license_file': 'Other Licenses',
            }

            for field, label in doc_map.items():
                file = request.files.get(field)

                if file and file.filename:
                    store_document(
                        file,
                        user_id,
                        master_id,
                        user_id,
                        'ufc_admin',
                        label
                    )

        mongo.db.users.update_one(
            {'_id': user_obj_id},
            {
                '$set': {
                    'approval_status': 'pending',
                    'latest_rejection_reason': '',
                    'state': form['state'],
                    'district': form['district'],
                    'block': form['block'],
                    'village': form['village'],
                    'updated_at': datetime.utcnow(),
                }
            }
        )

        mongo.db.validations.update_one(
            {
                'entity_id': user_id,
                'entity_type': 'ufc_admin_profile'
            },
            {
                '$set': {
                    'status': 'pending',
                    'rejection_reason': '',
                    'action_remarks': '',
                    'updated_at': datetime.utcnow(),
                },
                '$setOnInsert': {
                    'created_by_user_id': user_id,
                    'created_at': datetime.utcnow(),
                }
            },
            upsert=True
        )

        return jsonify({
            'ok': True,
            'message': 'Profile submitted for AVPL validation.',
            'approval_status': 'pending',
            'master_id': str(master_id)
        }), 200

    # WEB FLOW BELOW
    if not session.get('user_id'):
        return redirect(url_for('auth.login_select'))

    if session.get('role') != 'ufc_admin':
        return redirect(url_for('dashboard.home'))

    states = list_states()
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})

    if not user:
        flash('User not found. Please login again.', 'danger')
        return redirect(url_for('auth.logout'))

    master = mongo.db.ufc_admin_master.find_one({'linked_user_id': session['user_id']}) or {}
    latest_validation = _latest_validation_for_user(session['user_id'], 'ufc_admin_profile') or {}

    rejection_reason = (
        user.get('latest_rejection_reason')
        or latest_validation.get('rejection_reason')
        or latest_validation.get('action_remarks')
        or ''
    )

    if request.method == 'POST':
        owner_dob = request.form.get('owner_dob', '').strip()
        owner_age = calculate_age_from_dob(owner_dob)

        form = {
    'centre_uid': user.get('centre_uid'),
    'name_of_enterprise': request.form.get('name_of_enterprise', '').strip(),
    'name_of_owner': request.form.get('name_of_owner', '').strip(),
    'owner_dob': owner_dob,
    'owner_age': owner_age,
    'state': request.form.get('state', '').strip() or user.get('state', ''),
    'district': request.form.get('district', '').strip() or user.get('district', ''),
    'block': request.form.get('block', '').strip() or user.get('block', ''),
    'village': request.form.get('village', '').strip() or user.get('village', ''),
    'pan_number': request.form.get('pan_number', '').strip(),
    'gst_number': request.form.get('gst_number', '').strip(),
    'trader_license_number': request.form.get('trader_license_number', '').strip(),
    'other_licenses': request.form.get('other_licenses', '').strip(),
}
        
        if not form['owner_dob'] or not form['owner_age']:
            flash('Please enter a valid Owner Date of Birth.', 'danger')
            return render_template(
        'auth/complete_ufc_admin_profile.html',
        user=user,
        states=states,
        master=master,
        rejection_reason=rejection_reason,
        latest_validation=latest_validation
    )

        master_id = complete_ufc_admin_profile(session['user_id'], form)

        doc_map = {
            'registration_certificate': 'Registration Certificate',
            'pan_file': 'PAN',
            'gst_file': 'GST',
            'trader_license_file': 'Trader License',
            'other_license_file': 'Other Licenses',
        }

        for field, label in doc_map.items():
            file = request.files.get(field)
            if file and file.filename:
                store_document(
                    file,
                    session['user_id'],
                    master_id,
                    session['user_id'],
                    'ufc_admin',
                    label
                )

        mongo.db.users.update_one(
            {'_id': ObjectId(session['user_id'])},
            {
                '$set': {
                    'approval_status': 'pending',
                    'latest_rejection_reason': '',
                    'district': form['district'],
                    'block': form['block'],
                    'village': form['village'],
                    'state': form['state'],
                }
            }
        )

        mongo.db.validations.update_one(
            {
                'entity_id': session['user_id'],
                'entity_type': 'ufc_admin_profile'
            },
            {
                '$set': {
                    'status': 'pending',
                    'rejection_reason': '',
                    'action_remarks': ''
                }
            },
            upsert=False
        )

        session['approval_status'] = 'pending'
        session.pop('rejection_reason', None)

        flash('Profile resubmitted for AVPL validation.', 'success')
        return redirect(url_for('dashboard.pending_access'))

    return render_template(
        'auth/complete_ufc_admin_profile.html',
        user=user,
        states=states,
        master=master,
        rejection_reason=rejection_reason,
        latest_validation=latest_validation
    )


@auth_bp.route('/profile/ufc-mitra/complete', methods=['GET', 'POST'])
@login_required
def complete_ufc_mitra():
    is_app = request.form.get('app') == '1' or request.headers.get('Accept') == 'application/json'

    def _fail(message, status_code=400):
        if is_app:
            return jsonify({'ok': False, 'message': message}), status_code

        flash(message, 'danger')
        return _render_form()

    if session.get('role') != 'ufc_mitra':
        if is_app:
            return jsonify({'ok': False, 'message': 'Only UFC Mitra can complete this profile.'}), 403
        return redirect(url_for('dashboard.home'))

    states = list_states()
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})

    if not user:
        if is_app:
            return jsonify({'ok': False, 'message': 'User not found. Please login again.'}), 404

        flash('User not found. Please login again.', 'danger')
        return redirect(url_for('auth.logout'))

    master = mongo.db.ufc_mitra_master.find_one({'linked_user_id': session['user_id']}) or {}
    latest_validation = _latest_validation_for_user(session['user_id'], 'ufc_mitra_profile') or {}

    def _doc_value(doc):
        if not doc:
            return ''

        return (
            doc.get('file_path')
            or doc.get('filename')
            or doc.get('file_name')
            or doc.get('stored_name')
            or doc.get('path')
            or ''
        )

    def _doc_name(doc):
        if not doc:
            return ''

        return (
            doc.get('original_filename')
            or doc.get('original_name')
            or doc.get('display_name')
            or doc.get('filename')
            or doc.get('file_name')
            or doc.get('stored_name')
            or ''
        )

    def _latest_mitra_doc(label):
        master_id = master.get('_id')

        owner_terms = [
            {'user_id': session['user_id']},
            {'owner_user_id': session['user_id']},
            {'linked_user_id': session['user_id']},
            {'created_by_user_id': session['user_id']},
            {'uploaded_by': session['user_id']},
            {'entity_id': session['user_id']},
            {'entity_user_id': session['user_id']},
        ]

        try:
            user_obj_id = ObjectId(session['user_id'])
            owner_terms.extend([
                {'user_id': user_obj_id},
                {'owner_user_id': user_obj_id},
                {'linked_user_id': user_obj_id},
                {'created_by_user_id': user_obj_id},
                {'uploaded_by': user_obj_id},
                {'entity_id': user_obj_id},
                {'entity_user_id': user_obj_id},
            ])
        except Exception:
            pass

        if master_id:
            owner_terms.extend([
                {'master_id': master_id},
                {'record_id': master_id},
                {'entity_master_id': master_id},
                {'parent_id': master_id},
                {'entity_id': master_id},
            ])

            owner_terms.extend([
                {'master_id': str(master_id)},
                {'record_id': str(master_id)},
                {'entity_master_id': str(master_id)},
                {'parent_id': str(master_id)},
                {'entity_id': str(master_id)},
            ])

        label_terms = [
            {'label': label},
            {'document_label': label},
            {'document_type': label},
            {'doc_type': label},
            {'type': label},
            {'title': label},
            {'category': label},
        ]

        return mongo.db.documents.find_one(
            {
                '$and': [
                    {'$or': owner_terms},
                    {'$or': label_terms},
                ]
            },
            sort=[('updated_at', -1), ('created_at', -1), ('_id', -1)]
        )

    def _existing_mitra_documents():
        doc_map = {
            'government_id_file': 'Government-issued Identity Card',
            'education_certificate_file': 'Education Qualification Certificate',
            'passport_photo_file': 'Passport Size Photo',
        }

        result = {}

        for field, label in doc_map.items():
            doc = _latest_mitra_doc(label)

            result[field] = {
                'label': label,
                'name': _doc_name(doc),
                'path': _doc_value(doc),
                'url': _doc_value(doc),
                'exists': bool(_doc_value(doc)),
            }

        return result

    def _profile_payload():
        existing_docs = _existing_mitra_documents()

        return {
            'mitra_uid': user.get('mitra_uid') or master.get('mitra_uid') or '',
            'mapped_centre_uid': master.get('mapped_centre_uid') or user.get('mapped_centre_uid') or '',
            'name': master.get('name') or user.get('name') or '',
            'care_of': master.get('care_of') or user.get('care_of') or '',
            'dob': master.get('dob') or user.get('dob') or '',
            'age': master.get('age') or user.get('age') or '',
            'education': master.get('education') or user.get('education') or '',
            'gender': master.get('gender') or user.get('gender') or '',
            'government_id_number': master.get('government_id_number') or user.get('government_id_number') or '',
            'state': master.get('state') or user.get('state') or '',
            'district': master.get('district') or user.get('district') or '',
            'block': master.get('block') or user.get('block') or '',
            'village': master.get('village') or user.get('village') or '',
            'approval_status': user.get('approval_status') or 'pending_profile',
            'rejection_reason': rejection_reason,
            'existing_documents': existing_docs,
        }
    
    rejection_reason = (
        user.get('latest_rejection_reason')
        or latest_validation.get('rejection_reason')
        or latest_validation.get('action_remarks')
        or ''
    )

    def _render_form():
        return render_template(
            'auth/complete_ufc_mitra_profile.html',
            user=user,
            states=states,
            master=master,
            rejection_reason=rejection_reason,
            latest_validation=latest_validation
        )
    
    if request.method == 'GET' and is_app:
        return jsonify({
            'ok': True,
            'message': 'UFC Mitra correction profile loaded.',
            'profile': _profile_payload(),
            'master': _profile_payload(),
            'existing_documents': _existing_mitra_documents(),
            'latest_validation': {
                'status': latest_validation.get('status', ''),
                'rejection_reason': rejection_reason,
                'action_remarks': latest_validation.get('action_remarks', ''),
            }
        }), 200

    if request.method == 'POST':
        dob = request.form.get('dob', '').strip()
        calculated_age = calculate_age_from_dob(dob)

        form = {
            'mitra_uid': user.get('mitra_uid'),
            'mapped_centre_uid': request.form.get('mapped_centre_uid', '').strip(),
            'name': request.form.get('name', '').strip(),
            'care_of': request.form.get('care_of', '').strip(),
            'dob': dob,
            'age': calculated_age,
            'education': request.form.get('education', '').strip(),
            'gender': request.form.get('gender', '').strip(),
            'government_id_number': request.form.get('government_id_number', '').strip(),
            'state': request.form.get('state', '').strip() or user.get('state', ''),
            'district': request.form.get('district', '').strip() or user.get('district', ''),
            'block': request.form.get('block', '').strip() or user.get('block', ''),
            'village': request.form.get('village', '').strip() or user.get('village', ''),
        }

        if not form['dob'] or not form['age']:
            flash('Please enter a valid Date of Birth.', 'danger')
            return render_template(
                'auth/complete_ufc_mitra_profile.html',
                user=user,
                states=states,
                master=master,
                rejection_reason=rejection_reason,
                latest_validation=latest_validation
            )

        centre = (
            mongo.db.ufc_admin_master.find_one({'centre_uid': form['mapped_centre_uid']})
            or mongo.db.users.find_one({'centre_uid': form['mapped_centre_uid']})
        )

        if not centre:
            flash('Invalid UFC Admin Unique ID.', 'danger')
            return render_template(
                'auth/complete_ufc_mitra_profile.html',
                user=user,
                states=states,
                master=master,
                rejection_reason=rejection_reason,
                latest_validation=latest_validation
            )

        centre = (
            mongo.db.ufc_admin_master.find_one({'centre_uid': form['mapped_centre_uid']})
            or mongo.db.users.find_one({'centre_uid': form['mapped_centre_uid']})
        )

        if not centre:
            return _fail('Invalid UFC Admin Unique ID.', 400)

        master_id = complete_ufc_mitra_profile(session['user_id'], form)

        doc_map = {
            'government_id_file': 'Government-issued Identity Card',
            'education_certificate_file': 'Education Qualification Certificate',
            'passport_photo_file': 'Passport Size Photo',
        }

        existing_docs = _existing_mitra_documents()

        if is_app:
            for field, label in doc_map.items():
                uploaded_file = request.files.get(field)
                existing_doc = existing_docs.get(field) or {}

                has_uploaded_file = bool(uploaded_file and uploaded_file.filename)
                has_existing_file = bool(existing_doc.get('exists') or existing_doc.get('path'))

                if not has_uploaded_file and not has_existing_file:
                    return _fail(f'{label} is required.', 400)

        profile_photo_path = ''

        for field, label in doc_map.items():
            file = request.files.get(field)

            if not file or not file.filename:
                continue

            if field == 'passport_photo_file':
                allowed_image_types = {
                    'image/jpeg',
                    'image/png',
                    'image/jpg',
                    'image/webp'
                }

                file.seek(0, 2)
                file_size = file.tell()
                file.seek(0)

                file_ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
                allowed_extensions = {"jpg", "jpeg", "png", "webp"}

                if file.content_type not in allowed_image_types and file_ext not in allowed_extensions:
                    return _fail(
                        'Only JPG, PNG or WEBP image files are allowed for profile photo.',
                        400
                    )

                if file_size > 2 * 1024 * 1024:
                    return _fail(
                        'Profile photo must be less than or equal to 2 MB.',
                        400
                    )

            saved_doc = store_document(
                file,
                session['user_id'],
                master_id,
                session['user_id'],
                'ufc_mitra',
                label
            )

            saved_path = (
                saved_doc.get('file_path')
                or saved_doc.get('filename')
                or saved_doc.get('file_name')
                or saved_doc.get('stored_name')
                or ''
            ) if saved_doc else ''

            if saved_path:
                master_file_field_map = {
                    'government_id_file': 'government_id_file',
                    'education_certificate_file': 'education_certificate_file',
                    'passport_photo_file': 'passport_photo_file',
                }

                update_master_doc_field = master_file_field_map.get(field)

                if update_master_doc_field:
                    mongo.db.ufc_mitra_master.update_one(
                        {
                            '$or': [
                                {'linked_user_id': session['user_id']},
                                {'linked_user_id': ObjectId(session['user_id'])},
                                {'_id': master_id},
                            ]
                        },
                        {
                            '$set': {
                                update_master_doc_field: saved_path,
                                f'{update_master_doc_field}_name': file.filename,
                                'updated_at': datetime.utcnow()
                            }
                        }
                    )

            if field == 'passport_photo_file' and saved_path:
                profile_photo_path = saved_path

        update_user_doc = {
            'approval_status': 'pending',
            'latest_rejection_reason': '',
            'mapped_centre_uid': form['mapped_centre_uid'],
            'state': form['state'],
            'district': form['district'],
            'block': form['block'],
            'village': form['village'],
            'updated_at': datetime.utcnow()
        }

        update_master_doc = {
            'updated_at': datetime.utcnow()
        }

        if profile_photo_path:
            update_user_doc.update({
                'profile_photo': profile_photo_path,
            })

            update_master_doc.update({
                'profile_photo': profile_photo_path,
            })

        mongo.db.users.update_one(
            {'_id': ObjectId(session['user_id'])},
            {'$set': update_user_doc}
        )

        master_or = [
            {'linked_user_id': session['user_id']},
            {'linked_user_id': ObjectId(session['user_id'])},
        ]

        if master_id:
            master_or.append({'_id': master_id})
            if not isinstance(master_id, ObjectId):
                try:
                    master_or.append({'_id': ObjectId(master_id)})
                except Exception:
                    pass

        mongo.db.ufc_mitra_master.update_one(
            {'$or': master_or},
            {'$set': update_master_doc}
        )

        mongo.db.validations.update_one(
            {
                'entity_id': session['user_id'],
                'entity_type': 'ufc_mitra_profile'
            },
            {
                '$set': {
                    'status': 'pending',
                    'rejection_reason': '',
                    'action_remarks': ''
                }
            },
            upsert=False
        )

        session['approval_status'] = 'pending'
        session['mapped_centre_uid'] = form['mapped_centre_uid']
        session.pop('rejection_reason', None)

        if is_app:
            refreshed_master = mongo.db.ufc_mitra_master.find_one({'linked_user_id': session['user_id']}) or master

            return jsonify({
                'ok': True,
                'message': 'Profile submitted for AVPL validation.',
                'approval_status': 'pending',
                'master_id': str(master_id),
                'profile_photo': profile_photo_path,
                'profile': {
                    **_profile_payload(),
                    'approval_status': 'pending',
                },
                'existing_documents': _existing_mitra_documents(),
            }), 200

        flash('Profile resubmitted for AVPL validation.', 'success')
        return redirect(url_for('dashboard.pending_access'))

    return _render_form()

@auth_bp.route('/api/locations/states')
def api_states():
    return jsonify({'items': sorted(list_states())})


@auth_bp.route('/api/locations/districts')
def api_districts():
    return jsonify({'items': sorted(list_districts(request.args.get('state', '')))})


@auth_bp.route('/api/locations/blocks')
def api_blocks():
    return jsonify({'items': sorted(list_blocks(request.args.get('state', ''), request.args.get('district', '')))})


@auth_bp.route('/api/locations/villages')
def api_villages():
    return jsonify({'items': sorted(list_villages(request.args.get('state', ''), request.args.get('district', ''), request.args.get('block', '')))})


@auth_bp.route('/api/centre/<centre_uid>')
def api_centre(centre_uid):
    centre = mongo.db.ufc_admin_master.find_one({'centre_uid': centre_uid}) or mongo.db.users.find_one({'centre_uid': centre_uid})
    if not centre:
        return jsonify({'ok': False, 'message': 'Centre not found'}), 404
    return jsonify({'ok': True, 'centre_uid': centre_uid, 'state': centre.get('state', ''), 'district': centre.get('district', ''), 'block': centre.get('block', ''), 'village': centre.get('village', '')})


@auth_bp.route('/api/mitra/<mitra_uid>')
def api_mitra(mitra_uid):
    mitra = mongo.db.ufc_mitra_master.find_one({'mitra_uid': mitra_uid}) or mongo.db.users.find_one({'mitra_uid': mitra_uid})
    if not mitra:
        return jsonify({'ok': False, 'message': 'Mitra not found'}), 404
    centre_uid = mitra.get('mapped_centre_uid', '')
    centre = mongo.db.ufc_admin_master.find_one({'centre_uid': centre_uid}) or mongo.db.users.find_one({'centre_uid': centre_uid}) or {}
    return jsonify({'ok': True, 'mitra_uid': mitra_uid, 'centre_uid': centre_uid, 'state': mitra.get('state') or centre.get('state', ''), 'district': mitra.get('district') or centre.get('district', ''), 'block': mitra.get('block') or centre.get('block', ''), 'village': mitra.get('village') or centre.get('village', '')})
