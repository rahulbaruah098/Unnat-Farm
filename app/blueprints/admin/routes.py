from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.services.user_service import create_user
from app.services.audit_service import log_action
from app.services.document_service import store_document
from app.services.location_service import list_states
from app.services.accounting_product_mapping_service import (
    get_product_mapping_option_catalog,
    get_product_readiness_snapshot,
    upsert_product_mapping_request_from_product_master,
)
from datetime import datetime
from math import isfinite

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

def wants_json_response():
    return (
        request.headers.get("Accept") == "application/json"
        or request.is_json
        or request.args.get("format") == "json"
    )


def app_error(message, status=400):
    if wants_json_response():
        return jsonify({
            "ok": False,
            "message": message
        }), status

    flash(message, "danger")
    return None


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
            or q_lower in str(u.get("status", "")).lower()
        ]

    if wants_json_response():
        items = []

        for user in users:
            items.append({
                "_id": str(user.get("_id", "")),
                "name": user.get("name") or "-",
                "role": user.get("role") or "-",
                "username": user.get("username") or "-",
                "phone": user.get("phone") or user.get("contact_no") or user.get("mobile") or "-",
                "centre_uid": user.get("centre_uid") or user.get("mapped_centre_uid") or "-",
                "mapped_centre_uid": user.get("mapped_centre_uid") or user.get("centre_uid") or "-",
                "mitra_uid": user.get("mitra_uid") or user.get("mapped_mitra_uid") or "-",
                "mapped_mitra_uid": user.get("mapped_mitra_uid") or user.get("mitra_uid") or "-",
                "approval_status": user.get("approval_status") or user.get("status") or "pending_profile",
                "status": user.get("status") or user.get("approval_status") or "pending_profile",
            })

        return jsonify({
            "ok": True,
            "items": items,
            "q": q,
            "count": len(items)
        })

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

            if wants_json_response():
                return jsonify({"ok": False, "message": "You cannot create this role."}), 403
    
            flash('You cannot create this role.', 'danger')
            return render_template(
                'admin/create_user.html',
                allowed_roles=allowed_roles,
                states=states,
                centres=centres
            )

        if role == 'ufc_admin' and not extra.get('state'):

            if wants_json_response():
                return jsonify({"ok": False, "message": "State is required to generate Centre UID."}), 403
    
            flash('State is required to generate Centre UID.', 'danger')
            return render_template(
                'admin/create_user.html',
                allowed_roles=allowed_roles,
                states=states,
                centres=centres
            )

        if role == 'ufc_mitra' and not extra.get('mapped_centre_uid'):

            if wants_json_response():
                return jsonify({"ok": False, "message": "Mapped UnnatFarm Centre UID is required for UFC Mitra."}), 403
    
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
            if wants_json_response():
                return jsonify({"ok": False, "message": str(exc)}), 403
    

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

        generated_uid = user.get("centre_uid") or user.get("mitra_uid") or user.get("user_ref_id")

        if wants_json_response():
            safe_user = dict(user)
            if "_id" in safe_user:
                safe_user["_id"] = str(safe_user["_id"])

            return jsonify({
                "ok": True,
                "message": "User ID created successfully.",
                "generated_uid": generated_uid,
                "user": safe_user
            })

        flash(
            f'User ID created successfully. Generated UID: {generated_uid}',
            'success'
        )
        return redirect(url_for('admin.users'))

    return render_template(
        'admin/create_user.html',
        allowed_roles=allowed_roles,
        states=states,
        centres=centres
    )


def _delete_dummy_user_related_data(user):
    """
    Delete only dummy/test-series data.
    Normal real users are not affected by this helper.
    """
    if not user:
        return False

    if not user.get("is_dummy") and not user.get("dummy_group_id"):
        return False

    user_id = str(user["_id"])
    role = user.get("role")
    dummy_group_id = user.get("dummy_group_id")

    user_ids = {user_id}
    centre_uids = set()
    mitra_uids = set()
    farmer_ids = set()

    if role == "avpl_admin" and dummy_group_id:
        dummy_users = list(mongo.db.users.find({"dummy_group_id": dummy_group_id}))
        for dummy_user in dummy_users:
            user_ids.add(str(dummy_user["_id"]))

            if dummy_user.get("centre_uid"):
                centre_uids.add(dummy_user.get("centre_uid"))

            if dummy_user.get("mapped_centre_uid"):
                centre_uids.add(dummy_user.get("mapped_centre_uid"))

            if dummy_user.get("mitra_uid"):
                mitra_uids.add(dummy_user.get("mitra_uid"))

            if dummy_user.get("mapped_mitra_uid"):
                mitra_uids.add(dummy_user.get("mapped_mitra_uid"))

    elif role == "ufc_admin":
        centre_uid = user.get("centre_uid")
        if centre_uid:
            centre_uids.add(centre_uid)

        mitra_users = list(
            mongo.db.users.find(
                {
                    "$or": [
                        {"mapped_centre_uid": {"$in": list(centre_uids)}},
                        {"centre_uid": {"$in": list(centre_uids)}},
                    ],
                    "is_dummy": True,
                }
            )
        )

        for mitra_user in mitra_users:
            user_ids.add(str(mitra_user["_id"]))
            if mitra_user.get("mitra_uid"):
                mitra_uids.add(mitra_user.get("mitra_uid"))

    elif role == "ufc_mitra":
        mitra_uid = user.get("mitra_uid")
        if mitra_uid:
            mitra_uids.add(mitra_uid)

        if user.get("mapped_centre_uid"):
            centre_uids.add(user.get("mapped_centre_uid"))

    elif role == "farmer":
        if user.get("mapped_centre_uid"):
            centre_uids.add(user.get("mapped_centre_uid"))
        if user.get("mapped_mitra_uid"):
            mitra_uids.add(user.get("mapped_mitra_uid"))

    farmer_query = {
        "$or": [
            {"linked_user_id": {"$in": list(user_ids)}},
            {"centre_uid": {"$in": list(centre_uids)}},
            {"mitra_uid": {"$in": list(mitra_uids)}},
            {"dummy_group_id": dummy_group_id} if dummy_group_id and role == "avpl_admin" else {"_id": None},
        ],
        "is_dummy": True,
    }

    farmers = list(mongo.db.farmer_master.find(farmer_query))

    for farmer in farmers:
        farmer_ids.add(str(farmer["_id"]))
        if farmer.get("linked_user_id"):
            user_ids.add(str(farmer.get("linked_user_id")))

    common_user_filters = [
        {"user_id": {"$in": list(user_ids)}},
        {"farmer_user_id": {"$in": list(user_ids)}},
        {"buyer_user_id": {"$in": list(user_ids)}},
        {"seller_user_id": {"$in": list(user_ids)}},
        {"created_by": {"$in": list(user_ids)}},
        {"requested_by": {"$in": list(user_ids)}},
        {"submitted_by": {"$in": list(user_ids)}},
        {"uploaded_by_user_id": {"$in": list(user_ids)}},
        {"linked_user_id": {"$in": list(user_ids)}},
    ]

    common_scope_filters = []

    if centre_uids:
        common_scope_filters.append({"centre_uid": {"$in": list(centre_uids)}})
        common_scope_filters.append({"mapped_centre_uid": {"$in": list(centre_uids)}})

    if mitra_uids:
        common_scope_filters.append({"mitra_uid": {"$in": list(mitra_uids)}})
        common_scope_filters.append({"mapped_mitra_uid": {"$in": list(mitra_uids)}})

    if farmer_ids:
        common_scope_filters.append({"farmer_id": {"$in": list(farmer_ids)}})
        common_scope_filters.append({"buyer_farmer_id": {"$in": list(farmer_ids)}})

    if dummy_group_id and role == "avpl_admin":
        common_scope_filters.append({"dummy_group_id": dummy_group_id})

    delete_query = {
        "$or": common_user_filters + common_scope_filters
    }

    mongo.db.farmer_master.delete_many(delete_query)
    mongo.db.ufc_mitra_master.delete_many(delete_query)
    mongo.db.ufc_admin_master.delete_many(delete_query)
    mongo.db.validations.delete_many(delete_query)
    mongo.db.documents.delete_many(delete_query)
    mongo.db.orders.delete_many(delete_query)
    mongo.db.transactions.delete_many(delete_query)
    mongo.db.farmer_products.delete_many(delete_query)
    mongo.db.support_tickets.delete_many(delete_query)
    mongo.db.insurance_requests.delete_many(delete_query)
    mongo.db.financial_assistance_leads.delete_many(delete_query)
    mongo.db.mitra_product_purchases.delete_many(delete_query)
    mongo.db.mitra_product_sales.delete_many(delete_query)
    mongo.db.mitra_product_stock.delete_many(delete_query)
    mongo.db.profile_update_requests.delete_many(delete_query)
    mongo.db.audit_logs.delete_many(delete_query)

    mongo.db.users.delete_many(
        {
            "$or": [
                {"_id": {"$in": [ObjectId(uid) for uid in user_ids if ObjectId.is_valid(uid)]}},
                {"dummy_group_id": dummy_group_id} if dummy_group_id and role == "avpl_admin" else {"_id": None},
            ],
            "is_dummy": True,
        }
    )

    return True


@admin_bp.route('/users/<user_id>/delete', methods=['POST'])
@login_required
@roles_required('super_admin')
def delete_user(user_id):
    user = mongo.db.users.find_one({'_id': ObjectId(user_id)})

    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin.users'))

    deleted_dummy_series = _delete_dummy_user_related_data(user)

    if not deleted_dummy_series:
        mongo.db.users.delete_one({'_id': ObjectId(user_id)})

    log_action(session['user_id'], 'delete_user', 'user', user_id)

    if deleted_dummy_series:
        flash('Dummy test account and related dummy records deleted.', 'success')
    else:
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


PRODUCT_MASTER_ROLES = {"input", "output", "both"}
PRODUCT_METADATA_SOURCES = {
    "supplier_label",
    "manufacturer_catalogue",
    "manual_entry",
    "existing_internal_record",
}


def _clean_product_field(value, maximum=500):
    text = str(value or "").strip()
    return text[:maximum]


def _active_avpl_product_units():
    """Return active Accounting units for the AVPL product form."""
    entity = mongo.db.accounting_entities.find_one({
        'entity_code': 'AVPL',
        'entity_type': 'avpl',
        'status': 'active',
        'accounting_enabled': {'$ne': False},
        'is_deleted': {'$ne': True},
    })
    if not entity:
        return []

    rows = list(mongo.db.accounting_units.find({
        'accounting_entity_id': entity['_id'],
        'status': 'active',
        'is_active': True,
        'is_deleted': False,
    }).sort([('is_system', -1), ('name', 1), ('unit_code', 1)]))

    return [
        {
            'id': str(row['_id']),
            'unit_code': row.get('unit_code') or row.get('uqc_code') or '',
            'name': row.get('name') or row.get('unit_code') or 'Unit',
            'symbol': row.get('symbol') or '',
            'allows_fractional': row.get('allows_fractional') is True,
        }
        for row in rows
    ]


def _active_avpl_accounting_entity():
    return mongo.db.accounting_entities.find_one({
        'entity_code': 'AVPL',
        'entity_type': 'avpl',
        'status': 'active',
        'accounting_enabled': {'$ne': False},
        'is_deleted': {'$ne': True},
    })


def _attach_product_readiness(product, mapping=None):
    if not product:
        return product

    entity = _active_avpl_accounting_entity()

    if not entity:
        product['_readiness'] = {
            'status': 'accounting_unmapped',
            'label': 'Accounting Entity Unavailable',
            'tone': 'error',
            'product_master_ready': False,
            'accounting_ready': False,
            'commercial_ready': False,
            'stock_ready': False,
            'purchase_ready': False,
            'listing_ready': False,
            'sale_ready': False,
            'issues': [
                'The active AVPL Accounting entity is unavailable.'
            ],
            'primary_issue': (
                'The active AVPL Accounting entity is unavailable.'
            ),
        }
        return product

    try:
        product['_readiness'] = get_product_readiness_snapshot(
            entity['_id'],
            product['_id'],
            product_document=product,
            mapping_document=mapping,
        )
    except Exception as exc:
        product['_readiness'] = {
            'status': 'accounting_mapping_pending',
            'label': 'Readiness Check Failed',
            'tone': 'error',
            'product_master_ready': False,
            'accounting_ready': False,
            'commercial_ready': False,
            'stock_ready': False,
            'purchase_ready': False,
            'listing_ready': False,
            'sale_ready': False,
            'issues': [str(exc)],
            'primary_issue': str(exc),
        }

    return product



def _product_accounting_form_catalog(product=None):
    entity = _active_avpl_accounting_entity()
    if not entity:
        return {
            'entity': None,
            'hsn_masters': [],
            'units': [],
            'purchase_ledgers': [],
            'sales_ledgers': [],
            'inventory_ledgers': [],
            'mapping': None,
        }

    options = get_product_mapping_option_catalog(entity['_id'])
    mapping = None
    if product and product.get('_id'):
        mapping = mongo.db.accounting_product_mappings.find_one({
            'accounting_entity_id': entity['_id'],
            'source_product_id': product['_id'],
            'is_deleted': {'$ne': True},
            'status': {'$ne': 'cancelled'},
        })

    return {
        'entity': entity,
        'hsn_masters': options.get('hsn_masters') or [],
        'units': options.get('units') or [],
        'purchase_ledgers': options.get('purchase_ledgers') or [],
        'sales_ledgers': options.get('sales_ledgers') or [],
        'inventory_ledgers': options.get('inventory_ledgers') or [],
        'mapping': mapping,
    }



def _product_master_form_context(product=None, form_data=None):
    categories = list(mongo.db.product_categories.find({
        'is_deleted': {'$ne': True},
        'is_active': {'$ne': False},
    }).sort('name', 1))

    accounting_catalog = _product_accounting_form_catalog(product=product)
    mapping = accounting_catalog.get('mapping') or {}
    values = form_data or product or {}

    return {
        'categories': categories,
        'units': accounting_catalog.get('units') or _active_avpl_product_units(),
        'hsn_masters': accounting_catalog.get('hsn_masters') or [],
        'purchase_ledgers': accounting_catalog.get('purchase_ledgers') or [],
        'sales_ledgers': accounting_catalog.get('sales_ledgers') or [],
        'inventory_ledgers': accounting_catalog.get('inventory_ledgers') or [],
        'accounting_entity': accounting_catalog.get('entity'),
        'accounting_mapping': mapping,
        'product': product,
        'form_data': values,
        'metadata_sources': [
            ('supplier_label', 'Supplier / manufacturer label'),
            ('manufacturer_catalogue', 'Manufacturer catalogue'),
            ('manual_entry', 'Manual AVPL entry'),
            ('existing_internal_record', 'Existing internal record'),
        ],
    }




def _parse_non_negative_number(raw_value, field_label):
    try:
        value = float(str(raw_value or '0').strip() or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field_label} must be a valid number.') from exc
    if not isfinite(value):
        raise ValueError(f'{field_label} must be a finite number.')
    if value < 0:
        raise ValueError(f'{field_label} cannot be negative.')
    return value


def _build_product_accounting_mapping_form():
    return {
        'hsn_master_id': _clean_product_field(request.form.get('hsn_master_id'), 60),
        'base_unit_id': _clean_product_field(request.form.get('base_unit_id'), 60),
        'purchase_ledger_id': _clean_product_field(request.form.get('purchase_ledger_id'), 60),
        'sales_ledger_id': _clean_product_field(request.form.get('sales_ledger_id'), 60),
        'inventory_ledger_id': _clean_product_field(request.form.get('inventory_ledger_id'), 60),
        'inventory_tracking_enabled': request.form.get('inventory_tracking_enabled') == 'on',
        'purchase_enabled': request.form.get('purchase_enabled') == 'on',
        'sales_enabled': request.form.get('sales_enabled') == 'on',
        'mapping_note': _clean_product_field(request.form.get('mapping_note'), 1500),
    }


def _upsert_product_accounting_mapping(product_id):
    entity = _active_avpl_accounting_entity()
    if not entity:
        raise ValueError('The active AVPL Accounting entity is not available.')

    form = _build_product_accounting_mapping_form()
    required = {
        'hsn_master_id': 'HSN classification',
        'base_unit_id': 'base unit',
        'purchase_ledger_id': 'purchase ledger',
        'sales_ledger_id': 'sales ledger',
        'inventory_ledger_id': 'inventory ledger',
    }
    for key, label in required.items():
        if not form.get(key):
            raise ValueError(f'Select an approved {label}.')

    return upsert_product_mapping_request_from_product_master(
        entity['_id'],
        session.get('user_id'),
        product_id,
        form,
    )


def _build_product_master_payload(existing_product=None):
    existing_product = existing_product or {}
    name = _clean_product_field(request.form.get('name'), 240)
    category = _clean_product_field(request.form.get('category'), 160)
    product_role = _clean_product_field(
        request.form.get('product_role') or request.form.get('type'),
        30,
    ).lower()
    brand = _clean_product_field(request.form.get('brand'), 160)
    manufacturer = _clean_product_field(request.form.get('manufacturer'), 200)
    supplier_product_code = _clean_product_field(
        request.form.get('supplier_product_code'),
        120,
    )
    metadata_source = _clean_product_field(
        request.form.get('metadata_source') or 'manual_entry',
        60,
    ).lower()
    barcode = _clean_product_field(request.form.get('barcode'), 120)
    barcode_normalized = barcode.upper() if barcode else ''
    description = _clean_product_field(request.form.get('description'), 2000)
    base_unit_id_raw = _clean_product_field(request.form.get('base_unit_id'), 60)
    pack_size = _parse_non_negative_number(
        request.form.get('pack_size') or 1,
        'Pack size',
    )

    unnatfarm_eligible = request.form.get('unnatfarm_eligible') == 'on'

    if not name:
        raise ValueError('Product name is required.')
    if not category:
        raise ValueError('Product category is required.')
    if product_role not in PRODUCT_MASTER_ROLES:
        raise ValueError('Select a valid product role: Input, Output or Both.')
    if metadata_source not in PRODUCT_METADATA_SOURCES:
        raise ValueError('Select a valid metadata source.')
    if pack_size <= 0:
        raise ValueError('Pack size must be greater than 0.')

    category_row = mongo.db.product_categories.find_one({
        'name': category,
        'is_deleted': {'$ne': True},
        'is_active': {'$ne': False},
    })
    if not category_row:
        raise ValueError('The selected product category is not active.')

    try:
        base_unit_object_id = ObjectId(base_unit_id_raw)
    except Exception as exc:
        raise ValueError('Select a valid approved base unit.') from exc

    avpl_entity = mongo.db.accounting_entities.find_one({
        'entity_code': 'AVPL',
        'entity_type': 'avpl',
        'status': 'active',
        'accounting_enabled': {'$ne': False},
        'is_deleted': {'$ne': True},
    })
    if not avpl_entity:
        raise ValueError('The active AVPL Accounting entity is not available.')

    unit = mongo.db.accounting_units.find_one({
        '_id': base_unit_object_id,
        'accounting_entity_id': avpl_entity['_id'],
        'status': 'active',
        'is_active': True,
        'is_deleted': False,
    })
    if not unit:
        raise ValueError('The selected base unit is not active in Accounting.')

    duplicate_query = {
        'barcode_normalized': barcode_normalized,
        'is_deleted': {'$ne': True},
    }
    if existing_product.get('_id'):
        duplicate_query['_id'] = {'$ne': existing_product['_id']}
    if barcode_normalized and mongo.db.products.find_one(duplicate_query):
        raise ValueError('This barcode is already assigned to another product.')

    return {
        'name': name,
        'category': category,
        # Keep the legacy `type` field because existing farmer/UFC pages still
        # read it. `product_role` is the explicit Stage 1 master field.
        'type': product_role,
        'product_role': product_role,
        'brand': brand,
        'manufacturer': manufacturer,
        'supplier_product_code': supplier_product_code,
        'metadata_source': metadata_source,
        'barcode': barcode,
        'barcode_normalized': barcode_normalized,
        'description': description,
        'base_unit_id': unit['_id'],
        'base_unit_id_str': str(unit['_id']),
        'base_unit_code': unit.get('unit_code') or unit.get('uqc_code') or '',
        'base_unit_name': unit.get('name') or unit.get('unit_code') or '',
        'base_unit_symbol': unit.get('symbol') or '',
        'pack_size': pack_size,
        'unnatfarm_eligible': unnatfarm_eligible,
        'product_master_version': 3,
        'product_master_updated_by': session.get('user_id'),
        'product_master_updated_at': now_utc(),
        # Legacy commercial fields stay active until Stage 1 Batch 1.3.
        'updated_at': now_utc(),
    }


@admin_bp.route('/products/add', methods=['GET', 'POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def add_product():
    if request.method == 'POST':
        try:
            payload = _build_product_master_payload()
        except ValueError as exc:
            flash(str(exc), 'danger')
            context = _product_master_form_context(
                form_data=request.form,
            )
            return render_template('admin/add_product.html', **context), 400

        image_file = request.files.get('product_image')
        image_name = None
        if image_file and image_file.filename:
            doc = store_document(
                image_file,
                session['user_id'],
                None,
                session['user_id'],
                session['role'],
                'Product Image',
            )
            image_name = doc['filename'] if doc else None

        product_id = ObjectId()
        product_code = f"AVPL-P-{str(product_id)[-8:].upper()}"
        payload.update({
            '_id': product_id,
            'product_code': product_code,
            'image_name': image_name,
            'is_active': True,
            'is_deleted': False,
            'status': 'active',

            # Temporary compatibility defaults.
            # These are configured after the Product Master is saved.
            'price': '0',
            'available_quantity': 0,
            'available_centres': [],
            'commercial_setup_status': 'pending',
            'commercial_setup_version': 1,

            'created_by': session['user_id'],
            'created_at': now_utc(),
        })
        mongo.db.products.insert_one(payload)
        try:
            mapping_result = _upsert_product_accounting_mapping(product_id)
        except Exception as exc:
            mongo.db.products.delete_one({'_id': product_id})
            flash(f'Product was not created because the Accounting mapping failed: {exc}', 'danger')
            context = _product_master_form_context(
                form_data=request.form,
            )
            return render_template('admin/add_product.html', **context), 400

        mapping = mapping_result.get('mapping') or {}
        mongo.db.products.update_one(
            {'_id': product_id},
            {'$set': {
                'accounting_mapping_id': ObjectId(mapping['id']) if mapping.get('id') else None,
                'accounting_mapping_status': mapping.get('status') or 'active',
                'accounting_mapping_code': mapping.get('mapping_code') or '',
                'accounting_mapping_updated_at': now_utc(),
            }},
        )

        log_action(
            session['user_id'],
            'create_product_master',
            'product',
            str(product_id),
            metadata={
                'product_code': product_code,
                'product_name': payload.get('name'),
                'product_role': payload.get('product_role'),
                'base_unit_code': payload.get('base_unit_code'),
                'metadata_source': payload.get('metadata_source'),
            },
        )

        flash(
            (
                mapping_result.get('message')
                or 'Product master and Accounting mapping saved successfully.'
            )
            + ' Complete the separate commercial setup before using this '
            'product in the current catalogue.',
            'success',
        )

        return redirect(
            url_for(
                'admin.product_commercial_setup',
                product_id=product_id,
            )
        )

    context = _product_master_form_context()
    return render_template('admin/add_product.html', **context)


@admin_bp.route('/products/<product_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def edit_product(product_id):
    try:
        product_object_id = ObjectId(product_id)
    except Exception:
        flash('Invalid product reference.', 'danger')
        return redirect(url_for('admin.product_list'))

    product = mongo.db.products.find_one({
        '_id': product_object_id,
        'is_deleted': {'$ne': True},
    })
    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    if request.method == 'POST':
        try:
            payload = _build_product_master_payload(existing_product=product)
        except ValueError as exc:
            flash(str(exc), 'danger')
            context = _product_master_form_context(
                product=product,
                form_data=request.form,
            )
            return render_template('admin/add_product.html', **context), 400

        image_file = request.files.get('product_image')
        if image_file and image_file.filename:
            doc = store_document(
                image_file,
                session['user_id'],
                None,
                session['user_id'],
                session['role'],
                'Product Image',
            )
            if doc:
                payload['image_name'] = doc['filename']

        if not product.get('product_code'):
            payload['product_code'] = f"AVPL-P-{str(product['_id'])[-8:].upper()}"

        previous_product = dict(product)
        mongo.db.products.update_one({'_id': product['_id']}, {'$set': payload})
        try:
            mapping_result = _upsert_product_accounting_mapping(product['_id'])
        except Exception as exc:
            mongo.db.products.replace_one({'_id': product['_id']}, previous_product)
            flash(f'Product was not updated because the Accounting mapping failed: {exc}', 'danger')
            refreshed = mongo.db.products.find_one({'_id': product['_id']})
            context = _product_master_form_context(
                product=refreshed,
                form_data=request.form,
                
            )
            return render_template('admin/add_product.html', **context), 400

        mapping = mapping_result.get('mapping') or {}
        mongo.db.products.update_one(
            {'_id': product['_id']},
            {'$set': {
                'accounting_mapping_id': ObjectId(mapping['id']) if mapping.get('id') else None,
                'accounting_mapping_status': mapping.get('status') or 'active',
                'accounting_mapping_code': mapping.get('mapping_code') or '',
                'accounting_mapping_updated_at': now_utc(),
            }},
        )

        log_action(
            session['user_id'],
            'update_product_master',
            'product',
            product_id,
            metadata={
                'product_code': payload.get('product_code') or product.get('product_code'),
                'product_name': payload.get('name'),
                'product_role': payload.get('product_role'),
                'base_unit_code': payload.get('base_unit_code'),
                'metadata_source': payload.get('metadata_source'),
            },
        )

        flash(mapping_result.get('message') or 'Product master and Accounting mapping updated successfully.', 'success')
        return redirect(url_for('admin.product_list'))

    context = _product_master_form_context(product=product)
    return render_template('admin/add_product.html', **context)



@admin_bp.route(
    '/products/<product_id>/commercial-setup',
    methods=['GET', 'POST'],
)
@login_required
@roles_required('avpl_admin', 'accounts')
def product_commercial_setup(product_id):
    try:
        product_object_id = ObjectId(product_id)
    except Exception:
        flash('Invalid product reference.', 'danger')
        return redirect(url_for('admin.product_list'))

    product = mongo.db.products.find_one({
        '_id': product_object_id,
        'is_deleted': {'$ne': True},
    })

    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    accounting_catalog = _product_accounting_form_catalog(
        product=product,
    )

    _attach_product_readiness(
        product,
        accounting_catalog.get('mapping'),
    )

    centres = list(mongo.db.ufc_admin_master.find({
        'centre_uid': {
            '$exists': True,
            '$ne': '',
        },
    }).sort('centre_uid', 1))

    if request.method == 'POST':
        try:
            selling_price = _parse_non_negative_number(
                request.form.get('price'),
                'Selling price',
            )

            available_quantity = _parse_non_negative_number(
                request.form.get('available_quantity'),
                'Legacy available quantity',
            )
        except ValueError as exc:
            flash(str(exc), 'danger')

            return render_template(
                'admin/product_commercial_compatibility.html',
                product=product,
                centres=centres,
                selected_centres=request.form.getlist(
                    'available_centres'
                ),
                form_data=request.form,
            ), 400

        available_centres = [
            _clean_product_field(value, 80)
            for value in request.form.getlist('available_centres')
            if _clean_product_field(value, 80)
        ]

        if not available_centres:
            flash(
                'Select at least one Centre for the current '
                'legacy catalogue.',
                'danger',
            )

            return render_template(
                'admin/product_commercial_compatibility.html',
                product=product,
                centres=centres,
                selected_centres=[],
                form_data=request.form,
            ), 400

        if 'all' in available_centres:
            available_centres = ['all']
        else:
            valid_centre_uids = {
                str(row.get('centre_uid'))
                for row in centres
                if row.get('centre_uid')
            }

            available_centres = list(
                dict.fromkeys(available_centres)
            )

            invalid_centres = [
                value
                for value in available_centres
                if value not in valid_centre_uids
            ]

            if invalid_centres:
                flash(
                    'One or more selected Centres are invalid.',
                    'danger',
                )

                return render_template(
                    'admin/product_commercial_compatibility.html',
                    product=product,
                    centres=centres,
                    selected_centres=available_centres,
                    form_data=request.form,
                ), 400

        update = {
            'price': f'{selling_price:g}',
            'available_quantity': available_quantity,
            'available_centres': available_centres,
            'commercial_setup_status': 'configured',
            'commercial_setup_version': 1,
            'commercial_setup_updated_by': session.get('user_id'),
            'commercial_setup_updated_at': now_utc(),
            'updated_at': now_utc(),
        }

        mongo.db.products.update_one(
            {'_id': product_object_id},
            {'$set': update},
        )

        log_action(
            session['user_id'],
            'update_product_commercial_compatibility',
            'product',
            product_id,
            metadata={
                'product_code': product.get('product_code'),
                'selling_price': update['price'],
                'available_quantity': available_quantity,
                'available_centres': available_centres,
            },
        )

        flash(
            'Temporary commercial setup saved separately '
            'from the Product Master.',
            'success',
        )

        return redirect(url_for('admin.product_list'))

    selected_centres = product.get('available_centres') or []

    if isinstance(selected_centres, str):
        selected_centres = [selected_centres]

    return render_template(
        'admin/product_commercial_compatibility.html',
        product=product,
        centres=centres,
        selected_centres=selected_centres,
        form_data=product,
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
    'is_active': True,
    'is_deleted': False,
    'status': 'active',
    'created_by': session['user_id'],
    'created_at': now_utc(),
    'updated_at': now_utc()
})

        flash('Product category added.', 'success')
        return redirect(url_for('admin.product_categories'))

    categories = list(
    mongo.db.product_categories.find({
        'is_deleted': {'$ne': True}
    }).sort([
        ('created_at', 1),
        ('_id', 1)
    ])
)
    return render_template('admin/product_categories.html', categories=categories)


@admin_bp.route('/products/categories/<category_id>/toggle-status', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def toggle_product_category_status(category_id):
    category = mongo.db.product_categories.find_one({
        '_id': ObjectId(category_id),
        'is_deleted': {'$ne': True}
    })

    if not category:
        flash('Product category not found.', 'danger')
        return redirect(url_for('admin.product_categories'))

    current_active = category.get('is_active', True)
    new_active = not current_active
    new_status = 'active' if new_active else 'disabled'

    mongo.db.product_categories.update_one(
        {'_id': ObjectId(category_id)},
        {
            '$set': {
                'is_active': new_active,
                'status': new_status,
                'updated_at': now_utc(),
                'status_updated_at': now_utc(),
                'status_updated_by': session.get('user_id')
            }
        }
    )

    log_action(
        session['user_id'],
        'enable_product_category' if new_active else 'disable_product_category',
        'product_category',
        category_id,
        metadata={
            'category_name': category.get('name'),
            'new_status': new_status
        }
    )

    flash(
        'Product category enabled successfully.' if new_active else 'Product category disabled successfully.',
        'success'
    )
    return redirect(url_for('admin.product_categories'))


@admin_bp.route('/products/categories/<category_id>/delete', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def delete_product_category(category_id):
    category = mongo.db.product_categories.find_one({
        '_id': ObjectId(category_id),
        'is_deleted': {'$ne': True}
    })

    if not category:
        flash('Product category not found.', 'danger')
        return redirect(url_for('admin.product_categories'))

    mongo.db.product_categories.update_one(
        {'_id': ObjectId(category_id)},
        {
            '$set': {
                'is_deleted': True,
                'is_active': False,
                'status': 'deleted',
                'deleted_at': now_utc(),
                'deleted_by': session.get('user_id'),
                'updated_at': now_utc()
            }
        }
    )

    log_action(
        session['user_id'],
        'delete_product_category',
        'product_category',
        category_id,
        metadata={
            'category_name': category.get('name')
        }
    )

    flash('Product category deleted successfully.', 'success')
    return redirect(url_for('admin.product_categories'))


@admin_bp.route('/products')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def product_list():
    products = list(
    mongo.db.products.find({
        'is_deleted': {'$ne': True}
    }).sort('created_at', -1)
    )

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

    product_ids = [row['_id'] for row in products]
    mapping_by_product = {
        str(row.get('source_product_id')): row
        for row in mongo.db.accounting_product_mappings.find({
            'source_product_id': {'$in': product_ids},
            'is_deleted': {'$ne': True},
            'status': {'$ne': 'cancelled'},
        })
    } if product_ids else {}
    for row in products:
        mapping = mapping_by_product.get(str(row['_id']))
        row['_accounting_mapping'] = mapping
        _attach_product_readiness(
            row,
            mapping,
        )

    return render_template(
        'admin/product_list.html',
        products=products,
        farmer_products=farmer_products,
        centres=centres
    )

@admin_bp.route('/products/<product_id>/toggle-status', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def toggle_product_status(product_id):
    product = mongo.db.products.find_one({
        '_id': ObjectId(product_id),
        'is_deleted': {'$ne': True}
    })

    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    current_active = product.get('is_active', True)
    new_active = not current_active
    new_status = 'active' if new_active else 'disabled'

    mongo.db.products.update_one(
        {'_id': ObjectId(product_id)},
        {
            '$set': {
                'is_active': new_active,
                'status': new_status,
                'updated_at': now_utc(),
                'status_updated_at': now_utc(),
                'status_updated_by': session.get('user_id')
            }
        }
    )

    log_action(
        session['user_id'],
        'enable_product' if new_active else 'disable_product',
        'product',
        product_id,
        metadata={
            'product_name': product.get('name'),
            'new_status': new_status
        }
    )

    flash(
        'Product enabled successfully.' if new_active else 'Product disabled successfully.',
        'success'
    )
    return redirect(url_for('admin.product_list'))


@admin_bp.route('/products/<product_id>/delete', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def delete_product(product_id):
    product = mongo.db.products.find_one({
        '_id': ObjectId(product_id),
        'is_deleted': {'$ne': True}
    })

    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    mongo.db.products.update_one(
        {'_id': ObjectId(product_id)},
        {
            '$set': {
                'is_deleted': True,
                'is_active': False,
                'status': 'deleted',
                'deleted_at': now_utc(),
                'deleted_by': session.get('user_id'),
                'updated_at': now_utc()
            }
        }
    )

    log_action(
        session['user_id'],
        'delete_product',
        'product',
        product_id,
        metadata={
            'product_name': product.get('name')
        }
    )

    flash('Product deleted successfully.', 'success')
    return redirect(url_for('admin.product_list'))


@admin_bp.route('/products/<product_id>/restock', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def restock_product(product_id):
    if not current_app.config.get(
        'LEGACY_PRODUCT_RESTOCK_ENABLED',
        True,
    ):
        flash(
            'Direct product restocking is disabled. '
            'Use the approved AVPL purchase or '
            'stock-adjustment workflow.',
            'warning',
        )
        return redirect(url_for('admin.product_list'))

    try:
        product_object_id = ObjectId(product_id)
    except Exception:
        flash('Invalid product reference.', 'danger')
        return redirect(url_for('admin.product_list'))

    product = mongo.db.products.find_one({
        '_id': product_object_id,
        'is_deleted': {'$ne': True},
    })

    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    commercial_configured = (
        product.get('commercial_setup_status') == 'configured'
        or bool(product.get('available_centres'))
    )

    if not commercial_configured:
        flash(
            'Complete the separate Commercial Setup before '
            'using legacy refill.',
            'warning',
        )

        return redirect(
            url_for(
                'admin.product_commercial_setup',
                product_id=product_id,
            )
        )

    restock_quantity_raw = request.form.get(
        'restock_quantity',
        '',
    ).strip()

    try:
        restock_quantity = float(
            restock_quantity_raw or 0
        )
    except (TypeError, ValueError):
        restock_quantity = 0

    if (
        not isfinite(restock_quantity)
        or restock_quantity <= 0
    ):
        flash(
            'Please enter a valid restock quantity '
            'greater than 0.',
            'danger',
        )
        return redirect(url_for('admin.product_list'))

    current_quantity_raw = product.get(
        'available_quantity',
        0,
    )

    try:
        current_quantity = float(
            current_quantity_raw or 0
        )
    except (TypeError, ValueError):
        current_quantity = 0

    if not isfinite(current_quantity):
        current_quantity = 0

    new_quantity = (
        current_quantity
        + restock_quantity
    )

    timestamp = now_utc()

    mongo.db.products.update_one(
        {'_id': product_object_id},
        {
            '$set': {
                'available_quantity': new_quantity,
                'updated_at': timestamp,
                'last_restock_at': timestamp,
                'last_restock_by': session.get(
                    'user_id'
                ),
            },
            '$push': {
                'restock_history': {
                    'quantity_added': restock_quantity,
                    'previous_quantity': current_quantity,
                    'new_quantity': new_quantity,
                    'restocked_by': session.get(
                        'user_id'
                    ),
                    'restocked_at': timestamp,
                    'source': 'legacy_product_restock',
                    'stage': 'stage_1_compatibility',
                }
            },
        },
    )

    log_action(
        session['user_id'],
        'restock_product',
        'product',
        product_id,
        metadata={
            'quantity_added': restock_quantity,
            'previous_quantity': current_quantity,
            'new_quantity': new_quantity,
            'source': 'legacy_product_restock',
        },
    )

    flash(
        'Product restocked successfully. '
        f'New available quantity: {new_quantity:g}',
        'success',
    )

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