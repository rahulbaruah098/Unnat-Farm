from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.services.user_service import create_user
from app.services.audit_service import log_action
from app.services.document_service import store_document
from app.services.location_service import list_states
from datetime import datetime

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


@admin_bp.route('/users')
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_admin')
def users():
    query = {}

    if session.get('role') == 'avpl_admin':
        query['role'] = {'$ne': 'super_admin'}

    if session.get('role') == 'ufc_admin':
        query = {
            'role': 'ufc_mitra',
            'mapped_centre_uid': session.get('centre_uid')
        }

    users = list(mongo.db.users.find(query).sort('created_at', -1))

    q = request.args.get("q", "").strip()

    if q:
        q_lower = q.lower()
        users = [
            u for u in users
            if q_lower in str(u.get("name", "")).lower()
            or q_lower in str(u.get("role", "")).lower()
            or q_lower in str(u.get("username", "")).lower()
            or q_lower in str(u.get("phone", "")).lower()
            or q_lower in str(u.get("centre_uid", "")).lower()
            or q_lower in str(u.get("mapped_centre_uid", "")).lower()
            or q_lower in str(u.get("mitra_uid", "")).lower()
            or q_lower in str(u.get("mapped_mitra_uid", "")).lower()
            or q_lower in str(u.get("approval_status", "")).lower()
        ]

    return render_template('admin/user_list.html', users=users)



@admin_bp.route('/users/<user_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin')
def edit_user_view(user_id):
    user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users'))

    role = user.get('role')

    centres = list(
        mongo.db.ufc_admin_master.find(
            {'centre_uid': {'$exists': True, '$ne': ''}},
            {'centre_uid': 1, 'name': 1, 'name_of_enterprise': 1}
        ).sort('centre_uid', 1)
    )

    mitras = list(
        mongo.db.ufc_mitra_master.find(
            {
                '$or': [
                    {'mitra_uid': {'$exists': True, '$ne': ''}},
                    {'mapped_mitra_uid': {'$exists': True, '$ne': ''}}
                ]
            },
            {'mitra_uid': 1, 'mapped_mitra_uid': 1, 'name': 1, 'mapped_centre_uid': 1, 'centre_uid': 1}
        ).sort('mitra_uid', 1)
    )

    valid_centre_uids = {
        c.get('centre_uid')
        for c in centres
        if c.get('centre_uid')
    }

    valid_mitra_uids = {
        m.get('mitra_uid') or m.get('mapped_mitra_uid')
        for m in mitras
        if m.get('mitra_uid') or m.get('mapped_mitra_uid')
    }

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip() or None
        phone = request.form.get('phone', '').strip() or None
        mapped_centre_uid = request.form.get('mapped_centre_uid', '').strip()
        mapped_mitra_uid = request.form.get('mapped_mitra_uid', '').strip()

        if not name:
            flash('Name is required.', 'danger')
            return render_template(
                'admin/edit_user.html',
                user=user,
                centres=centres,
                mitras=mitras
            )

        if username:
            existing_username = mongo.db.users.find_one({
                'username': username,
                '_id': {'$ne': ObjectId(user_id)}
            })

            if existing_username:
                flash('Username already exists for another user.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    user=user,
                    centres=centres,
                    mitras=mitras
                )

        if phone:
            existing_phone = mongo.db.users.find_one({
                'phone': phone,
                '_id': {'$ne': ObjectId(user_id)}
            })

            if existing_phone:
                flash('Phone number already exists for another user.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    user=user,
                    centres=centres,
                    mitras=mitras
                )

        user_set = {
            'name': name,
            'username': username,
            'phone': phone,
            'updated_at': now_utc()
        }

        if role == 'ufc_mitra':
            if not mapped_centre_uid:
                flash('Please select mapped centre for UFC Mitra.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    user=user,
                    centres=centres,
                    mitras=mitras
                )

            if mapped_centre_uid not in valid_centre_uids:
                flash('Selected centre is invalid.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    user=user,
                    centres=centres,
                    mitras=mitras
                )

            user_set['mapped_centre_uid'] = mapped_centre_uid

        if role == 'farmer':
            if not mapped_mitra_uid:
                flash('Please select mapped Mitra UID for farmer.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    user=user,
                    centres=centres,
                    mitras=mitras
                )

            if mapped_mitra_uid not in valid_mitra_uids:
                flash('Selected Mitra UID is invalid.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    user=user,
                    centres=centres,
                    mitras=mitras
                )

            selected_mitra = next(
                (
                    m for m in mitras
                    if (m.get('mitra_uid') or m.get('mapped_mitra_uid')) == mapped_mitra_uid
                ),
                {}
            )

            user_set['mapped_mitra_uid'] = mapped_mitra_uid
            user_set['mapped_centre_uid'] = (
                selected_mitra.get('mapped_centre_uid')
                or selected_mitra.get('centre_uid')
                or user.get('mapped_centre_uid')
            )

        mongo.db.users.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': user_set}
        )

        linked_user_id = str(user['_id'])

        common_master_update = {
            'name': name,
            'phone': phone,
            'contact_no': phone,
            'updated_at': now_utc()
        }

        if role == 'ufc_admin':
            mongo.db.ufc_admin_master.update_many(
                {'linked_user_id': linked_user_id},
                {'$set': common_master_update}
            )

        elif role == 'ufc_mitra':
            mitra_filters = [
                {'linked_user_id': linked_user_id},
                {'linked_user_id': str(user['_id'])},
                {'mitra_uid': user.get('mitra_uid')},
                {'mapped_mitra_uid': user.get('mapped_mitra_uid')}
            ]

            try:
                mitra_filters.append({'linked_user_id': ObjectId(linked_user_id)})
            except Exception:
                pass

            mitra_filters = [
                f for f in mitra_filters
                if list(f.values())[0]
            ]

            mongo.db.ufc_mitra_master.update_many(
                {'$or': mitra_filters},
                {
                    '$set': {
                        **common_master_update,
                        'mapped_centre_uid': mapped_centre_uid,
                        'centre_uid': mapped_centre_uid
                    }
                }
            )

        elif role == 'farmer':
            farmer_filters = [
                {'linked_user_id': linked_user_id},
                {'linked_user_id': str(user['_id'])},
                {'phone': user.get('phone')},
                {'contact_no': user.get('phone')}
            ]

            try:
                farmer_filters.append({'linked_user_id': ObjectId(linked_user_id)})
            except Exception:
                pass

            farmer_filters = [
                f for f in farmer_filters
                if list(f.values())[0]
            ]

            mongo.db.farmer_master.update_many(
                {'$or': farmer_filters},
                {
                    '$set': {
                        **common_master_update,
                        'mitra_uid': mapped_mitra_uid,
                        'mapped_mitra_uid': mapped_mitra_uid,
                        'centre_uid': user_set.get('mapped_centre_uid'),
                        'mapped_centre_uid': user_set.get('mapped_centre_uid')
                    }
                }
            )

        log_action(session['user_id'], 'edit_user', 'user', user_id)
        flash('User details updated successfully.', 'success')
        return redirect(url_for('admin.users'))

    return render_template(
        'admin/edit_user.html',
        user=user,
        centres=centres,
        mitras=mitras
    )

@admin_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_admin')
def create_user_view():
    role_ctx = session.get('role')

    allowed_roles = [
        'avpl_admin',
        'accounts',
        'sales_nelocals',
        'sales_unnatfarm',
        'ufc_admin',
        'ufc_mitra'
    ]

    if role_ctx == 'avpl_admin':
        allowed_roles = ['accounts', 'sales_nelocals', 'sales_unnatfarm', 'ufc_admin']

    if role_ctx == 'ufc_admin':
        allowed_roles = ['ufc_mitra']

    states = list_states()

    centres = list(
        mongo.db.ufc_admin_master.find(
            {'centre_uid': {'$exists': True, '$ne': ''}},
            {'centre_uid': 1, 'name': 1, 'name_of_enterprise': 1}
        ).sort('centre_uid', 1)
    )

    if request.method == 'POST':
        role = request.form.get('role')
        name = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip() or None
        phone = request.form.get('phone', '').strip() or None
        password = request.form.get('password', '').strip()

        extra = {
            'state': request.form.get('state', '').strip() if role == 'ufc_admin' else '',
            'district': '',
            'block': '',
            'village': '',
            'mapped_centre_uid': request.form.get('mapped_centre_uid', '').strip(),
        }

        if role_ctx == 'ufc_admin' and role == 'ufc_mitra':
            extra['mapped_centre_uid'] = session.get('centre_uid')

            centre = (
                mongo.db.ufc_admin_master.find_one({'centre_uid': session.get('centre_uid')})
                or mongo.db.users.find_one({'centre_uid': session.get('centre_uid')})
                or {}
            )

            for k in ['state', 'district', 'block', 'village']:
                extra[k] = centre.get(k, '')

        if role not in allowed_roles:
            flash('You cannot create this role.', 'danger')
            return render_template(
                'admin/create_user.html',
                allowed_roles=allowed_roles,
                states=states,
                centres=centres
            )

        if role == 'ufc_admin' and not extra.get('state'):
            flash('State is required to generate Centre UID.', 'danger')
            return render_template(
                'admin/create_user.html',
                allowed_roles=allowed_roles,
                states=states,
                centres=centres
            )

        if role == 'ufc_mitra' and not extra.get('mapped_centre_uid'):
            flash('Mapped UnnatFarm Centre UID is required for UFC Mitra.', 'danger')
            return render_template(
                'admin/create_user.html',
                allowed_roles=allowed_roles,
                states=states,
                centres=centres
            )

        try:
            user = create_user(
                name,
                role,
                username=username,
                phone=phone,
                password=password,
                created_by=session['user_id'],
                extra=extra
            )
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template(
                'admin/create_user.html',
                allowed_roles=allowed_roles,
                states=states,
                centres=centres
            )

        if role == 'ufc_admin':
            mongo.db.ufc_admin_master.update_one(
                {'linked_user_id': str(user['_id'])},
                {
                    '$set': {
                        'linked_user_id': str(user['_id']),
                        'centre_uid': user['centre_uid'],
                        'state': user.get('state'),
                        'district': user.get('district'),
                        'block': user.get('block'),
                        'village': user.get('village'),
                        'approval_status': 'pending_profile',
                        'created_at': now_utc(),
                        'updated_at': now_utc(),
                    }
                },
                upsert=True,
            )

        if role == 'ufc_mitra':
            mongo.db.ufc_mitra_master.update_one(
                {'linked_user_id': str(user['_id'])},
                {
                    '$set': {
                        'linked_user_id': str(user['_id']),
                        'mitra_uid': user['mitra_uid'],
                        'mapped_centre_uid': user.get('mapped_centre_uid'),
                        'state': user.get('state'),
                        'district': user.get('district'),
                        'block': user.get('block'),
                        'village': user.get('village'),
                        'approval_status': 'pending_profile',
                        'created_at': now_utc(),
                        'updated_at': now_utc(),
                    }
                },
                upsert=True,
            )

        log_action(
            session['user_id'],
            'create_user',
            'user',
            str(user['_id']),
            metadata={'role': role}
        )

        flash(
            f'User ID created successfully. Generated UID: {user.get("centre_uid") or user.get("mitra_uid") or user.get("user_ref_id")}',
            'success'
        )
        return redirect(url_for('admin.users'))

    return render_template(
        'admin/create_user.html',
        allowed_roles=allowed_roles,
        states=states,
        centres=centres
    )


@admin_bp.route('/users/<user_id>/delete', methods=['POST'])
@login_required
@roles_required('super_admin')
def delete_user(user_id):
    mongo.db.users.delete_one({'_id': ObjectId(user_id)})
    log_action(session['user_id'], 'delete_user', 'user', user_id)
    flash('User deleted.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/<user_id>/disable', methods=['POST'])
@login_required
@roles_required('super_admin')
def disable_user(user_id):
    if str(session.get('user_id')) == str(user_id):
        flash('You cannot disable your own account.', 'danger')
        return redirect(url_for('admin.edit_user_view', user_id=user_id))

    user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users'))

    mongo.db.users.update_one(
        {'_id': ObjectId(user_id)},
        {
            '$set': {
                'active': False,
                'status': 'disabled',
                'updated_at': now_utc(),
                'disabled_at': now_utc(),
                'disabled_by': session.get('user_id')
            }
        }
    )

    log_action(session['user_id'], 'disable_user', 'user', user_id)

    flash('User account disabled successfully.', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/users/<user_id>/enable', methods=['POST'])
@login_required
@roles_required('super_admin')
def enable_user(user_id):
    user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users'))

    mongo.db.users.update_one(
        {'_id': ObjectId(user_id)},
        {
            '$set': {
                'active': True,
                'status': 'active',
                'updated_at': now_utc(),
                'enabled_at': now_utc(),
                'enabled_by': session.get('user_id')
            },
            '$unset': {
                'disabled_at': '',
                'disabled_by': ''
            }
        }
    )

    log_action(session['user_id'], 'enable_user', 'user', user_id)

    flash('User account enabled successfully.', 'success')
    return redirect(url_for('admin.edit_user_view', user_id=user_id))

@admin_bp.route('/users/<user_id>/reset-password', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin')
def reset_user_password(user_id):
    user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users'))

    if request.method == 'POST':
        new_password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not new_password:
            flash('New password is required.', 'danger')
            return redirect(url_for('admin.reset_user_password', user_id=user_id))

        if len(new_password) < 6:
            flash('Password must be at least 6 characters long.', 'danger')
            return redirect(url_for('admin.reset_user_password', user_id=user_id))

        if new_password != confirm_password:
            flash('Password and confirm password do not match.', 'danger')
            return redirect(url_for('admin.reset_user_password', user_id=user_id))

        old_password_hash = (
            user.get('password_hash')
            or user.get('password')
            or user.get('passwordHash')
            or ''
        )

        if old_password_hash and check_password_hash(old_password_hash, new_password):
            flash('New password cannot be the same as the current password.', 'danger')
            return redirect(url_for('admin.reset_user_password', user_id=user_id))

        mongo.db.users.update_one(
            {'_id': ObjectId(user_id)},
            {
                '$set': {
                    'password_hash': generate_password_hash(new_password),
                    'updated_at': now_utc()
                }
            }
        )

        log_action(session['user_id'], 'reset_password', 'user', user_id)
        flash('Password reset successfully.', 'success')
        return redirect(url_for('admin.users'))

    return render_template('admin/reset_password.html', user=user)


@admin_bp.route('/lms/upload', methods=['GET', 'POST'])
@login_required
@roles_required('avpl_admin', 'sales_nelocals', 'sales_unnatfarm')
def lms_upload():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        audience = request.form.get('audience', '').strip()
        lms_type = request.form.get('lms_type', '').strip()
        activity_category = request.form.get('activity_category', 'all').strip()

        file = request.files.get('material_file')
        filename = None

        if file and file.filename:
            doc = store_document(
                file,
                session['user_id'],
                None,
                session['user_id'],
                session['role'],
                'LMS Material'
            )
            filename = doc['filename'] if doc else None

        mongo.db.lms_materials.insert_one({
            'title': title,
            'description': description,
            'audience': audience,
            'lms_type': lms_type,
            'activity_category': activity_category,
            'file_name': filename,
            'created_by': session['user_id'],
            'created_role': session.get('role'),
            'created_at': now_utc()
        })

        flash('LMS material uploaded successfully.', 'success')
        return redirect(url_for('admin.lms_upload'))

    items = list(mongo.db.lms_materials.find({}).sort('created_at', -1).limit(20))
    return render_template('admin/lms_upload.html', items=items)


@admin_bp.route('/products/add', methods=['GET', 'POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def add_product():
    categories = list(mongo.db.product_categories.find({}).sort('name', 1))
    centres = list(mongo.db.ufc_admin_master.find({
        'centre_uid': {'$exists': True, '$ne': ''}
    }).sort('centre_uid', 1))

    if request.method == 'POST':
        image_file = request.files.get('product_image')
        image_name = None

        if image_file and image_file.filename:
            doc = store_document(
                image_file,
                session['user_id'],
                None,
                session['user_id'],
                session['role'],
                'Product Image'
            )
            image_name = doc['filename'] if doc else None

        available_quantity = request.form.get('available_quantity', '').strip()

        try:
            available_quantity = float(available_quantity or 0)
        except ValueError:
            available_quantity = 0

        mongo.db.products.insert_one({
            'name': request.form.get('name', '').strip(),
            'category': request.form.get('category', '').strip(),
            'type': request.form.get('type', '').strip(),
            'available_centres': request.form.getlist('available_centres'),
            'price': request.form.get('price', '').strip(),
            'available_quantity': available_quantity,
            'image_name': image_name,
            'created_by': session['user_id'],
            'created_at': now_utc()
        })

        flash('Product added.', 'success')
        return redirect(url_for('admin.product_list'))

    return render_template(
        'admin/add_product.html',
        categories=categories,
        centres=centres
    )

@admin_bp.route('/products/categories', methods=['GET', 'POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def product_categories():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()

        if not name:
            flash('Category name is required.', 'danger')
            return redirect(url_for('admin.product_categories'))

        existing = mongo.db.product_categories.find_one({
            'name': {'$regex': f'^{name}$', '$options': 'i'}
        })

        if existing:
            flash('This category already exists.', 'danger')
            return redirect(url_for('admin.product_categories'))

        mongo.db.product_categories.insert_one({
            'name': name,
            'created_by': session['user_id'],
            'created_at': now_utc()
        })

        flash('Product category added.', 'success')
        return redirect(url_for('admin.product_categories'))

    categories = list(
    mongo.db.product_categories.find({}).sort([
        ('created_at', 1),
        ('_id', 1)
    ])
)
    return render_template('admin/product_categories.html', categories=categories)

@admin_bp.route('/products')
@login_required
def product_list():
    products = list(mongo.db.products.find({}).sort('created_at', -1))

    farmer_products = list(
        mongo.db.farmer_products
        .find({"status": "active"})
        .sort("created_at", -1)
    )

    centres = list(
        mongo.db.ufc_admin_master
        .find({}, {"centre_uid": 1, "name": 1, "name_of_enterprise": 1})
        .sort("centre_uid", 1)
    )

    return render_template(
        'admin/product_list.html',
        products=products,
        farmer_products=farmer_products,
        centres=centres
    )

@admin_bp.route('/products/<product_id>/restock', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def restock_product(product_id):
    product = mongo.db.products.find_one({'_id': ObjectId(product_id)})

    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    restock_quantity_raw = request.form.get('restock_quantity', '').strip()

    try:
        restock_quantity = float(restock_quantity_raw or 0)
    except ValueError:
        restock_quantity = 0

    if restock_quantity <= 0:
        flash('Please enter a valid restock quantity greater than 0.', 'danger')
        return redirect(url_for('admin.product_list'))

    current_quantity_raw = product.get('available_quantity', 0)

    try:
        current_quantity = float(current_quantity_raw or 0)
    except (TypeError, ValueError):
        current_quantity = 0

    new_quantity = current_quantity + restock_quantity

    mongo.db.products.update_one(
        {'_id': ObjectId(product_id)},
        {
            '$set': {
                'available_quantity': new_quantity,
                'updated_at': now_utc(),
                'last_restock_at': now_utc(),
                'last_restock_by': session.get('user_id')
            },
            '$push': {
                'restock_history': {
                    'quantity_added': restock_quantity,
                    'previous_quantity': current_quantity,
                    'new_quantity': new_quantity,
                    'restocked_by': session.get('user_id'),
                    'restocked_at': now_utc()
                }
            }
        }
    )

    log_action(
        session['user_id'],
        'restock_product',
        'product',
        product_id,
        metadata={
            'quantity_added': restock_quantity,
            'previous_quantity': current_quantity,
            'new_quantity': new_quantity
        }
    )

    flash(f'Product restocked successfully. New available quantity: {new_quantity:g}', 'success')
    return redirect(url_for('admin.product_list'))

@admin_bp.route('/traders/onboard', methods=['GET', 'POST'])
@login_required
@roles_required('sales_nelocals')
def onboard_trader():
    if request.method == 'POST':
        business_name = request.form.get('business_name', '').strip()
        contact_person = request.form.get('contact_person', '').strip()
        phone = request.form.get('phone', '').strip()
        address = request.form.get('address', '').strip()

        if not business_name:
            flash('Business name is required.', 'danger')
            return redirect(url_for('admin.onboard_trader'))

        if not contact_person:
            flash('Contact person is required.', 'danger')
            return redirect(url_for('admin.onboard_trader'))

        if not phone:
            flash('Phone number is required.', 'danger')
            return redirect(url_for('admin.onboard_trader'))

        if not address:
            flash('Address is required.', 'danger')
            return redirect(url_for('admin.onboard_trader'))

        existing_phone = mongo.db.trader_onboarding.find_one({
            'phone': phone
        })

        if existing_phone:
            flash('This phone number is already registered with another trader.', 'danger')
            return redirect(url_for('admin.onboard_trader'))

        mongo.db.trader_onboarding.insert_one({
            'business_name': business_name,
            'contact_person': contact_person,
            'phone': phone,
            'address': address,
            'status': 'pending',
            'created_by': session['user_id'],
            'created_at': now_utc()
        })

        flash('Trader onboarding saved.', 'success')
        return redirect(url_for('admin.onboard_trader'))

    traders = list(mongo.db.trader_onboarding.find({}).sort('created_at', -1))
    return render_template('admin/trader_onboarding.html', traders=traders)

@admin_bp.route('/mitra-bonus-settings', methods=['GET', 'POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def mitra_bonus_settings():
    if request.method == 'POST':
        bonus_type = request.form.get('bonus_type')
        category = request.form.get('category') or 'all'
        mitra_uid = request.form.get('mitra_uid') or None
        percentage = float(request.form.get('percentage') or 2)

        mongo.db.mitra_bonus_settings.insert_one({
            "bonus_type": bonus_type,
            "category": category,
            "mitra_uid": mitra_uid,
            "percentage": percentage,
            "created_at": now_utc(),
            "updated_at": now_utc()
        })

        flash("Bonus setting saved.", "success")
        return redirect(url_for('admin.mitra_bonus_settings'))

    q = request.args.get("q", "").strip()

    settings = list(
    mongo.db.mitra_bonus_settings.find({}).sort([
        ('created_at', 1),
        ('_id', 1)
    ])
)


    if q:
        q_lower = q.lower()
        settings = [
            s for s in settings
            if q_lower in str(s.get("bonus_type", "")).lower()
            or q_lower in str(s.get("category", "")).lower()
            or q_lower in str(s.get("mitra_uid", "") or "All").lower()
            or q_lower in str(s.get("percentage", "")).lower()
        ]

    mitras = list(mongo.db.ufc_mitra_master.find({}).sort('name', 1))

    return render_template(
        'admin/mitra_bonus_settings.html',
        settings=settings,
        mitras=mitras
    )
    
@admin_bp.route('/ufc-mitra-earnings')
@login_required
@roles_required('avpl_admin', 'accounts')
def ufc_mitra_earnings():
    selected_month = request.args.get('month', '').strip()
    selected_mitra_uid = request.args.get('mitra_uid', '').strip()

    mitras = list(mongo.db.ufc_mitra_master.find({}).sort('name', 1))

    date_filter = {}

    if selected_month:
        year, month = map(int, selected_month.split('-'))
        start_date = datetime(year, month, 1)

        if month == 12:
            end_date = datetime(year + 1, 1, 1)
        else:
            end_date = datetime(year, month + 1, 1)

        date_filter = {
            'created_at': {
                '$gte': start_date,
                '$lt': end_date
            }
        }

    rows = []

    for mitra in mitras:
        mitra_uid = mitra.get('mitra_uid')

        if selected_mitra_uid and selected_mitra_uid != mitra_uid:
            continue

        base_filter = {
            'mitra_uid': mitra_uid
        }

        monthly_filter = {
            **base_filter,
            **date_filter
        }

        pos_sales = list(mongo.db.pos_sales.find(monthly_filter))
        farmer_sales = list(mongo.db.farmer_product_sales.find(monthly_filter))

        total_pos_sales = list(mongo.db.pos_sales.find(base_filter))
        total_farmer_sales = list(mongo.db.farmer_product_sales.find(base_filter))

        monthly_avpl_earning = sum(float(s.get('bonus_amount') or 0) for s in pos_sales)
        monthly_farmer_earning = sum(float(s.get('bonus_amount') or 0) for s in farmer_sales)

        total_avpl_earning = sum(float(s.get('bonus_amount') or 0) for s in total_pos_sales)
        total_farmer_earning = sum(float(s.get('bonus_amount') or 0) for s in total_farmer_sales)

        rows.append({
            'mitra_name': mitra.get('name'),
            'mitra_uid': mitra_uid,
            'linked_user_id': mitra.get('linked_user_id'),
            'monthly_avpl_earning': monthly_avpl_earning,
            'monthly_farmer_earning': monthly_farmer_earning,
            'monthly_total_earning': monthly_avpl_earning + monthly_farmer_earning,
            'total_avpl_earning': total_avpl_earning,
            'total_farmer_earning': total_farmer_earning,
            'total_earning': total_avpl_earning + total_farmer_earning
        })

    return render_template(
        'admin/ufc_mitra_earnings.html',
        rows=rows,
        mitras=mitras,
        selected_month=selected_month,
        selected_mitra_uid=selected_mitra_uid
    )    

@admin_bp.route('/mitra-profile/<mitra_uid>')
@login_required
@roles_required('avpl_admin', 'accounts')
def view_mitra_profile(mitra_uid):
    mitra = mongo.db.ufc_mitra_master.find_one({
        'mitra_uid': mitra_uid
    }) or mongo.db.ufc_mitra_master.find_one({
        'mapped_mitra_uid': mitra_uid
    })

    if not mitra:
        flash('Mitra profile not found.', 'danger')
        return redirect(url_for('admin.ufc_mitra_earnings'))

    linked_user_id = mitra.get('linked_user_id')

    user = None
    if linked_user_id:
        try:
            user = mongo.db.users.find_one({'_id': ObjectId(linked_user_id)})
        except Exception:
            user = None

        if not user:
            user = mongo.db.users.find_one({'_id': linked_user_id})

    if not user:
        user = mongo.db.users.find_one({
            '$or': [
                {'mitra_uid': mitra_uid},
                {'mapped_mitra_uid': mitra_uid},
                {'username': mitra.get('phone')},
                {'phone': mitra.get('phone')},
            ]
        }) or {}

    doc_query = []

    if linked_user_id:
        doc_query.append({'linked_user_id': str(linked_user_id)})
        try:
            doc_query.append({'linked_user_id': ObjectId(linked_user_id)})
        except Exception:
            pass

    if user and user.get('_id'):
        doc_query.append({'linked_user_id': str(user['_id'])})
        doc_query.append({'user_id': str(user['_id'])})
        doc_query.append({'uploaded_by': str(user['_id'])})

    if mitra.get('_id'):
        doc_query.append({'linked_master_id': str(mitra['_id'])})
        doc_query.append({'linked_master_id': mitra['_id']})

    docs = list(
        mongo.db.documents.find({'$or': doc_query}).sort('created_at', -1)
    ) if doc_query else []

    return render_template(
        'modules/profile.html',
        user=user,
        master=mitra,
        docs=docs,
        pending_profile_update=None,
        readonly_profile=True,
        profile_title='UFC Mitra Profile'
    )