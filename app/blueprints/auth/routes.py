from bson import ObjectId
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

    # ---------- APP LOGIN ----------
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

        if role != 'ufc_admin':
            return jsonify({'ok': False, 'message': 'Only UFC Admin allowed in app'}), 403

        update_last_login(str(user['_id']))

        latest_validation = _latest_validation_for_user(str(user['_id']), 'ufc_admin_profile') or {}

        return jsonify({
            'ok': True,
            'message': 'Login successful',
            'user': {
                'id': str(user['_id']),
                'username': user.get('username') or user.get('name') or identifier,
                'role': role,
                'centre_uid': user.get('centre_uid') or '',
                'approval_status': user.get('approval_status') or 'pending_profile',
                'rejection_reason': (
                    user.get('latest_rejection_reason')
                    or latest_validation.get('rejection_reason')
                    or latest_validation.get('action_remarks')
                    or ''
                ),
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


@auth_bp.route('/register/farmer', methods=['GET', 'POST'])
def register_farmer():
    states = list_states()
    if request.method == 'POST':
        form = {
            'name': request.form.get('name', '').strip(),
            'gender': request.form.get('gender', '').strip(),
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
        try:
            create_farmer_registration(form)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template('auth/register_farmer.html', data=form, states=states)
        flash('Farmer registration submitted. Wait for UFC Mitra validation.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('auth/register_farmer.html', states=states)

#changes by atlanta
@auth_bp.route('/profile/ufc-admin/complete', methods=['GET', 'POST'])
def complete_ufc_admin():
    is_json = request.is_json or request.headers.get('Content-Type') == 'application/json'

    if is_json:
        data = request.get_json(silent=True) or {}
        user_id = (data.get('user_id') or '').strip()

        if not user_id:
            return jsonify({'ok': False, 'message': 'User ID is required.'}), 400

        user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

        if not user:
            return jsonify({'ok': False, 'message': 'User not found.'}), 404

        if (user.get('role') or '').strip().lower() != 'ufc_admin':
            return jsonify({'ok': False, 'message': 'Invalid user role.'}), 403

        form = {
            'centre_uid': user.get('centre_uid'),
            'name_of_enterprise': (data.get('name_of_enterprise') or '').strip(),
            'name_of_owner': (data.get('name_of_owner') or '').strip(),
            'state': (data.get('state') or user.get('state') or '').strip(),
            'district': (data.get('district') or user.get('district') or '').strip(),
            'block': (data.get('block') or user.get('block') or '').strip(),
            'village': (data.get('village') or user.get('village') or '').strip(),
            'pan_number': (data.get('pan_number') or '').strip(),
            'gst_number': (data.get('gst_number') or '').strip(),
            'trader_license_number': (data.get('trader_license_number') or '').strip(),
            'other_licenses': (data.get('other_licenses') or '').strip(),
        }

        if not form['name_of_enterprise'] or not form['name_of_owner']:
            return jsonify({'ok': False, 'message': 'Enterprise name and owner name are required.'}), 400

        master_id = complete_ufc_admin_profile(user_id, form)

        mongo.db.users.update_one(
            {'_id': ObjectId(user_id)},
            {
                '$set': {
                    'approval_status': 'pending',
                    'latest_rejection_reason': '',
                    'state': form['state'],
                    'district': form['district'],
                    'block': form['block'],
                    'village': form['village'],
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
                    'action_remarks': ''
                }
            },
            upsert=False
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
        form = {
            'centre_uid': user.get('centre_uid'),
            'name_of_enterprise': request.form.get('name_of_enterprise', '').strip(),
            'name_of_owner': request.form.get('name_of_owner', '').strip(),
            'state': request.form.get('state', '').strip() or user.get('state', ''),
            'district': request.form.get('district', '').strip() or user.get('district', ''),
            'block': request.form.get('block', '').strip() or user.get('block', ''),
            'village': request.form.get('village', '').strip() or user.get('village', ''),
            'pan_number': request.form.get('pan_number', '').strip(),
            'gst_number': request.form.get('gst_number', '').strip(),
            'trader_license_number': request.form.get('trader_license_number', '').strip(),
            'other_licenses': request.form.get('other_licenses', '').strip(),
        }

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
    if session.get('role') != 'ufc_mitra':
        return redirect(url_for('dashboard.home'))

    states = list_states()
    user = mongo.db.users.find_one({'_id': ObjectId(session['user_id'])})

    if not user:
        flash('User not found. Please login again.', 'danger')
        return redirect(url_for('auth.logout'))

    master = mongo.db.ufc_mitra_master.find_one({'linked_user_id': session['user_id']}) or {}
    latest_validation = _latest_validation_for_user(session['user_id'], 'ufc_mitra_profile') or {}

    rejection_reason = (
        user.get('latest_rejection_reason')
        or latest_validation.get('rejection_reason')
        or latest_validation.get('action_remarks')
        or ''
    )

    if request.method == 'POST':
        form = {
            'mitra_uid': user.get('mitra_uid'),
            'mapped_centre_uid': request.form.get('mapped_centre_uid', '').strip(),
            'name': request.form.get('name', '').strip(),
            'care_of': request.form.get('care_of', '').strip(),
            'dob': request.form.get('dob', '').strip(),
            'age': request.form.get('age', '').strip(),
            'education': request.form.get('education', '').strip(),
            'gender': request.form.get('gender', '').strip(),
            'government_id_number': request.form.get('government_id_number', '').strip(),
            'state': request.form.get('state', '').strip() or user.get('state', ''),
            'district': request.form.get('district', '').strip() or user.get('district', ''),
            'block': request.form.get('block', '').strip() or user.get('block', ''),
            'village': request.form.get('village', '').strip() or user.get('village', ''),
        }

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

        master_id = complete_ufc_mitra_profile(session['user_id'], form)

        doc_map = {
            'government_id_file': 'Government-issued Identity Card',
            'education_certificate_file': 'Education Qualification Certificate',
            'passport_photo_file': 'Passport Size Photo',
        }

        for field, label in doc_map.items():
            file = request.files.get(field)
            if file and file.filename:
                store_document(
                    file,
                    session['user_id'],
                    master_id,
                    session['user_id'],
                    'ufc_mitra',
                    label
                )

        mongo.db.users.update_one(
            {'_id': ObjectId(session['user_id'])},
            {
                '$set': {
                    'approval_status': 'pending',
                    'latest_rejection_reason': '',
                    'mapped_centre_uid': form['mapped_centre_uid'],
                    'state': form['state'],
                    'district': form['district'],
                    'block': form['block'],
                    'village': form['village'],
                }
            }
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

        flash('Profile resubmitted for AVPL validation.', 'success')
        return redirect(url_for('dashboard.pending_access'))

    return render_template(
        'auth/complete_ufc_mitra_profile.html',
        user=user,
        states=states,
        master=master,
        rejection_reason=rejection_reason,
        latest_validation=latest_validation
    )

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
