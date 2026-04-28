from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.services.user_service import create_user
from app.services.audit_service import log_action
from app.services.document_service import store_document
from app.services.location_service import list_states

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


@admin_bp.route('/users')
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_admin')
def users():
    query = {}
    if session.get('role') == 'avpl_admin':
        query['role'] = {'$ne': 'super_admin'}
    if session.get('role') == 'ufc_admin':
        query = {'role': 'ufc_mitra', 'mapped_centre_uid': session.get('centre_uid')}
    users = list(mongo.db.users.find(query).sort('created_at', -1))
    return render_template('admin/user_list.html', users=users)


@admin_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_admin')
def create_user_view():
    role_ctx = session.get('role')
    allowed_roles = ['avpl_admin', 'accounts', 'sales_nelocals', 'sales_unnatfarm', 'ufc_admin']
    if role_ctx == 'avpl_admin':
        allowed_roles = ['accounts', 'sales_nelocals', 'sales_unnatfarm', 'ufc_admin']
    if role_ctx == 'ufc_admin':
        allowed_roles = ['ufc_mitra']

    states = list_states()
    if request.method == 'POST':
        role = request.form.get('role')
        name = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip() or None
        phone = request.form.get('phone', '').strip() or None
        password = request.form.get('password', '').strip()
        # During user/ID creation, AVPL selects only State for UFC Admin.
        # District, block and village are typed manually later in profile completion.
        # For UFC Mitra, only the mapped Centre UID is required at creation.
        extra = {
            'state': request.form.get('state', '').strip() if role == 'ufc_admin' else '',
            'district': '',
            'block': '',
            'village': '',
            'mapped_centre_uid': request.form.get('mapped_centre_uid', '').strip(),
        }
        if role_ctx == 'ufc_admin' and role == 'ufc_mitra':
            extra['mapped_centre_uid'] = session.get('centre_uid')
            centre = mongo.db.ufc_admin_master.find_one({'centre_uid': session.get('centre_uid')}) or mongo.db.users.find_one({'centre_uid': session.get('centre_uid')}) or {}
            for k in ['state', 'district', 'block', 'village']:
                extra[k] = centre.get(k, '')
        if role not in allowed_roles:
            flash('You cannot create this role.', 'danger')
            return render_template('admin/create_user.html', allowed_roles=allowed_roles, states=states)
        if role == 'ufc_admin' and not extra.get('state'):
            flash('State is required to generate Centre UID.', 'danger')
            return render_template('admin/create_user.html', allowed_roles=allowed_roles, states=states)
        if role == 'ufc_mitra' and not extra.get('mapped_centre_uid'):
            flash('Mapped UnnatFarm Centre UID is required for UFC Mitra.', 'danger')
            return render_template('admin/create_user.html', allowed_roles=allowed_roles, states=states)
        try:
            user = create_user(name, role, username=username, phone=phone, password=password, created_by=session['user_id'], extra=extra)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template('admin/create_user.html', allowed_roles=allowed_roles, states=states)

        if role == 'ufc_admin':
            mongo.db.ufc_admin_master.update_one(
                {'linked_user_id': str(user['_id'])},
                {'$set': {
                    'linked_user_id': str(user['_id']), 'centre_uid': user['centre_uid'],
                    'state': user.get('state'), 'district': user.get('district'), 'block': user.get('block'), 'village': user.get('village'),
                    'approval_status': 'pending_profile', 'created_at': now_utc(), 'updated_at': now_utc(),
                }}, upsert=True,
            )
        if role == 'ufc_mitra':
            mongo.db.ufc_mitra_master.update_one(
                {'linked_user_id': str(user['_id'])},
                {'$set': {
                    'linked_user_id': str(user['_id']), 'mitra_uid': user['mitra_uid'], 'mapped_centre_uid': user.get('mapped_centre_uid'),
                    'state': user.get('state'), 'district': user.get('district'), 'block': user.get('block'), 'village': user.get('village'),
                    'approval_status': 'pending_profile', 'created_at': now_utc(), 'updated_at': now_utc(),
                }}, upsert=True,
            )
        log_action(session['user_id'], 'create_user', 'user', str(user['_id']), metadata={'role': role})
        flash(f'User ID created successfully. Generated UID: {user.get("centre_uid") or user.get("mitra_uid") or user.get("user_ref_id")}', 'success')
        return redirect(url_for('admin.users'))
    return render_template('admin/create_user.html', allowed_roles=allowed_roles, states=states)


@admin_bp.route('/users/<user_id>/delete', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def delete_user(user_id):
    mongo.db.users.delete_one({'_id': ObjectId(user_id)})
    log_action(session['user_id'], 'delete_user', 'user', user_id)
    flash('User deleted.', 'success')
    return redirect(url_for('admin.users'))


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
            return render_template('admin/reset_password.html', user=user)
        if new_password != confirm_password:
            flash('Password and confirm password do not match.', 'danger')
            return render_template('admin/reset_password.html', user=user)
        mongo.db.users.update_one({'_id': ObjectId(user_id)}, {'$set': {'password_hash': generate_password_hash(new_password), 'updated_at': now_utc()}})
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
    if request.method == 'POST':
        mongo.db.products.insert_one({'name': request.form.get('name', '').strip(), 'category': request.form.get('category', '').strip(), 'type': request.form.get('type', '').strip(), 'available_centres': request.form.get('available_centres', '').strip(), 'price': request.form.get('price', '').strip(), 'created_at': now_utc()})
        flash('Product added.', 'success')
        return redirect(url_for('admin.product_list'))
    return render_template('admin/add_product.html')


@admin_bp.route('/products')
@login_required
def product_list():
    products = list(mongo.db.products.find({}).sort('created_at', -1))
    return render_template('admin/product_list.html', products=products)


@admin_bp.route('/traders/onboard', methods=['GET', 'POST'])
@login_required
@roles_required('sales_nelocals')
def onboard_trader():
    if request.method == 'POST':
        mongo.db.trader_onboarding.insert_one({'business_name': request.form.get('business_name'), 'contact_person': request.form.get('contact_person'), 'phone': request.form.get('phone'), 'address': request.form.get('address'), 'status': 'pending', 'created_by': session['user_id'], 'created_at': now_utc()})
        flash('Trader onboarding saved.', 'success')
        return redirect(url_for('admin.onboard_trader'))
    traders = list(mongo.db.trader_onboarding.find({}).sort('created_at', -1))
    return render_template('admin/trader_onboarding.html', traders=traders)
