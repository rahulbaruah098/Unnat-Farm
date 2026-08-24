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
from app.services.accounting_party_ledger_service import (
    approve_party_ledger,
    cancel_party_ledger,
    create_supplier_from_operational_master,
    deactivate_party_ledger,
    get_supplier_master_overview,
    reactivate_party_ledger,
    return_party_ledger,
    submit_party_ledger,
    update_supplier_from_operational_master,
    withdraw_party_ledger,
)
from app.services.avpl_purchase_order_service import (
    approve_purchase_order,
    cancel_purchase_order,
    create_purchase_order,
    get_purchase_order,
    get_purchase_order_form_catalog,
    get_purchase_order_overview,
    return_purchase_order,
    submit_purchase_order,
    update_purchase_order,
    withdraw_purchase_order,
)
from app.services.avpl_goods_receipt_service import (
    attach_goods_receipt_supplier_invoice_document,
    cancel_goods_receipt,
    create_goods_receipt,
    get_goods_receipt,
    get_goods_receipt_form_catalog,
    get_goods_receipt_overview,
    get_goods_receipts_for_purchase_order,
    post_goods_receipt,
    return_goods_receipt,
    submit_goods_receipt,
    update_goods_receipt,
)
from app.services.avpl_supplier_invoice_service import (
    cancel_supplier_invoice,
    create_supplier_invoice,
    get_supplier_invoice,
    get_supplier_invoice_form_catalog,
    get_supplier_invoice_overview,
    get_supplier_invoices_for_purchase_order,
    update_supplier_invoice,
)
from app.services.avpl_purchase_posting_service import (
    get_purchase_invoice_print_context,
    post_supplier_invoice_purchase,
    prepare_supplier_invoice_posting,
)
from app.services.workflow_policy_service import workflow_is_streamlined
from app.services.avpl_inventory_service import (
    approve_stock_adjustment,
    create_stock_adjustment,
    get_batch_expiry_overview,
    get_current_stock_overview,
    get_marketplace_publication_map,
    get_product_inventory_snapshot_map,
    get_stock_adjustment_overview,
    get_stock_movement_overview,
    publish_products_to_ufc,
    reject_stock_adjustment,
)
from app.services.avpl_ufc_order_service import (
    approve_ufc_order,
    cancel_approved_ufc_order,
    dispatch_ufc_order,
    get_avpl_order_overview,
    get_order as get_avpl_ufc_order,
    reject_ufc_order,
)
from app.services.avpl_ufc_sales_service import (
    bulk_sync_existing_orders,
    ensure_sales_documents_for_order,
    get_avpl_sale,
    get_avpl_sales_overview,
    get_sales_invoice_print_context,
)
from app.services.payment_service import (
    confirm_reported_payment as stage8_confirm_reported_payment,
    get_avpl_payment_overview,
    get_payment_receipt_context,
    record_payment as stage8_record_payment,
    reject_reported_payment as stage8_reject_reported_payment,
    reverse_payment as stage8_reverse_payment,
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
            'issues': ['The active AVPL Accounting entity is unavailable.'],
            'primary_issue': 'The active AVPL Accounting entity is unavailable.',
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
    reorder_level = _parse_non_negative_number(
        request.form.get('reorder_level') or 0,
        'Reorder level',
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
        'reorder_level': reorder_level,
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
            + ' The product is not visible to UFC Centres until you publish it from View All Products.',
            'success',
        )
        return redirect(url_for('admin.product_list'))

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
    # Stage 3 retires the old per-product Centre/quantity commercial screen.
    # Keep the endpoint as a compatibility redirect so old bookmarks do not
    # break, but no stock or marketplace data can be edited here anymore.
    flash(
        'The old Commercial Setup has been retired. Select products on View All Products and use Publish to UFC Marketplace.',
        'info',
    )
    return redirect(url_for('admin.product_list'))


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

    product_ids = [row['_id'] for row in products]
    mapping_by_product = {
        str(row.get('source_product_id')): row
        for row in mongo.db.accounting_product_mappings.find({
            'source_product_id': {'$in': product_ids},
            'is_deleted': {'$ne': True},
            'status': {'$ne': 'cancelled'},
        })
    } if product_ids else {}

    entity = _active_avpl_accounting_entity()
    inventory_by_product = {}
    publication_by_product = {}
    if entity and product_ids:
        try:
            inventory_by_product = get_product_inventory_snapshot_map(
                entity['_id'], product_ids
            )
            publication_by_product = get_marketplace_publication_map(
                entity['_id'], product_ids
            )
        except Exception:
            # Product Master must remain usable even if inventory is temporarily
            # unavailable. The page will show zero/unpublished rather than fail.
            inventory_by_product = {}
            publication_by_product = {}

    for row in products:
        product_key = str(row['_id'])
        mapping = mapping_by_product.get(product_key)
        row['_accounting_mapping'] = mapping
        _attach_product_readiness(row, mapping)
        row['_inventory'] = inventory_by_product.get(product_key) or {
            'physical_quantity': '0',
            'reserved_quantity': '0',
            'saleable_quantity': '0',
            'damaged_quantity': '0',
            'expired_quantity': '0',
            'lot_count': 0,
            'warehouse_count': 0,
            'warehouses': [],
            'has_stock': False,
            'has_saleable_stock': False,
        }
        publication = publication_by_product.get(product_key) or {}
        row['_ufc_publication'] = publication
        row['_ufc_published'] = publication.get('status') == 'published'

    return render_template(
        'admin/product_list.html',
        products=products,
    )

@admin_bp.route('/products/<product_id>/toggle-status', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def toggle_product_status(product_id):
    try:
        product_object_id = ObjectId(product_id)
    except Exception:
        flash('Invalid product reference.', 'danger')
        return redirect(url_for('admin.product_list'))

    product = mongo.db.products.find_one({
        '_id': product_object_id,
        'is_deleted': {'$ne': True}
    })
    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    current_active = product.get('is_active', True)
    new_active = not current_active
    new_status = 'active' if new_active else 'disabled'
    timestamp = now_utc()
    mongo.db.products.update_one(
        {'_id': product_object_id},
        {'$set': {
            'is_active': new_active,
            'status': new_status,
            'updated_at': timestamp,
            'status_updated_at': timestamp,
            'status_updated_by': session.get('user_id')
        }}
    )

    # Disabled products must never remain visible in the UFC Marketplace.
    if not new_active:
        entity = _active_avpl_accounting_entity()
        if entity:
            try:
                publish_products_to_ufc(
                    entity['_id'], session.get('user_id'), [product_object_id], publish=False
                )
            except Exception:
                # The Product status change remains authoritative; future UFC
                # queries must also require product.is_active=True.
                pass

    log_action(
        session['user_id'],
        'enable_product' if new_active else 'disable_product',
        'product',
        product_id,
        metadata={'product_name': product.get('name'), 'new_status': new_status}
    )
    flash(
        'Product enabled successfully.' if new_active else 'Product disabled and removed from UFC Marketplace.',
        'success'
    )
    return redirect(url_for('admin.product_list'))


@admin_bp.route('/products/<product_id>/delete', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def delete_product(product_id):
    try:
        product_object_id = ObjectId(product_id)
    except Exception:
        flash('Invalid product reference.', 'danger')
        return redirect(url_for('admin.product_list'))

    product = mongo.db.products.find_one({
        '_id': product_object_id,
        'is_deleted': {'$ne': True}
    })
    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('admin.product_list'))

    entity = _active_avpl_accounting_entity()
    if entity:
        try:
            stock = get_product_inventory_snapshot_map(entity['_id'], [product_object_id]).get(product_id) or {}
            physical = float(stock.get('physical_quantity') or 0)
        except Exception:
            physical = 0
        if physical > 0:
            flash(
                'This product still has physical AVPL stock. Do not delete it. Disable/unpublish it, or clear stock through an approved transaction first.',
                'warning',
            )
            return redirect(url_for('admin.product_list'))

    # Remove marketplace visibility before soft-delete. The publication service
    # intentionally ignores already-deleted Product Masters.
    if entity:
        try:
            publish_products_to_ufc(
                entity['_id'], session.get('user_id'), [product_object_id], publish=False
            )
        except Exception:
            pass

    timestamp = now_utc()
    mongo.db.products.update_one(
        {'_id': product_object_id},
        {'$set': {
            'is_deleted': True,
            'is_active': False,
            'status': 'deleted',
            'deleted_at': timestamp,
            'deleted_by': session.get('user_id'),
            'updated_at': timestamp
        }}
    )

    log_action(
        session['user_id'], 'delete_product', 'product', product_id,
        metadata={'product_name': product.get('name')}
    )
    flash('Product deleted successfully.', 'success')
    return redirect(url_for('admin.product_list'))


@admin_bp.route('/products/<product_id>/restock', methods=['POST'])
@login_required
@roles_required('avpl_admin', 'accounts')
def restock_product(product_id):
    # Compatibility endpoint only. Stage 3 makes inventory transaction-owned;
    # direct edits to products.available_quantity are permanently disabled.
    flash(
        'Manual refill is disabled. Stock must come from Goods Receipt or an approved Stock Adjustment.',
        'warning',
    )
    return redirect(url_for('admin.current_stock'))


# ---------------------------------------------------------------------------
# Stage 2 — AVPL Supplier Master and Purchase Orders
# ---------------------------------------------------------------------------


def _stage2_entity_or_redirect():
    entity = _active_avpl_accounting_entity()
    if not entity:
        flash('The active AVPL Accounting entity is unavailable.', 'danger')
    return entity


@admin_bp.route('/procurement')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def procurement_home():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    actor_user_id = session.get('user_id')
    supplier_overview = get_supplier_master_overview(entity['_id'], actor_user_id)
    po_overview = get_purchase_order_overview(entity['_id'], actor_user_id)
    grn_overview = get_goods_receipt_overview(entity['_id'], actor_user_id)
    invoice_overview = get_supplier_invoice_overview(entity['_id'], actor_user_id)

    return render_template(
        'admin/procurement_home.html',
        supplier_overview=supplier_overview,
        po_overview=po_overview,
        grn_overview=grn_overview,
        invoice_overview=invoice_overview,
    )


@admin_bp.route('/inventory/current-stock')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def current_stock():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    try:
        overview = get_current_stock_overview(
            entity['_id'],
            query_text=request.args.get('q', ''),
            warehouse_code=request.args.get('warehouse', ''),
        )
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'query': request.args.get('q', ''),
            'selected_warehouse': request.args.get('warehouse', ''),
            'warehouses': [],
            'summary': {
                'product_rows': 0,
                'warehouse_count': 0,
                'low_stock_count': 0,
                'expired_lot_count': 0,
                'stock_value': '0.00',
            },
            'cost_basis_note': '',
        }

    return render_template('admin/current_stock.html', overview=overview)



@admin_bp.route('/inventory/batch-expiry')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def batch_expiry():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        overview = get_batch_expiry_overview(
            entity['_id'],
            query_text=request.args.get('q', ''),
            warehouse_code=request.args.get('warehouse', ''),
            status_filter=request.args.get('status', 'all'),
        )
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'summary': {'total_lots': 0, 'expired': 0, 'expiring_soon': 0, 'healthy': 0, 'untracked': 0},
            'query': request.args.get('q', ''),
            'selected_warehouse': request.args.get('warehouse', ''),
            'selected_status': request.args.get('status', 'all'),
            'warehouses': [],
            'expiring_days': 30,
        }
    return render_template('admin/batch_expiry.html', overview=overview)


@admin_bp.route('/inventory/stock-movements')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def stock_movements():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        overview = get_stock_movement_overview(
            entity['_id'],
            query_text=request.args.get('q', ''),
            movement_type=request.args.get('type', ''),
            page=request.args.get('page', 1, type=int) or 1,
        )
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'query': request.args.get('q', ''),
            'selected_type': request.args.get('type', ''),
            'movement_types': [],
            'pagination': {'page': 1, 'total': 0, 'total_pages': 1, 'has_prev': False, 'has_next': False},
        }
    return render_template('admin/stock_movements.html', overview=overview)


@admin_bp.route('/inventory/adjustments', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def stock_adjustments():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    if request.method == 'POST':
        proof_file = request.files.get('proof')
        if not proof_file or not proof_file.filename:
            flash('Attach supporting proof before submitting a stock adjustment.', 'danger')
            return redirect(url_for('admin.stock_adjustments'))

        adjustment_id = ObjectId()
        proof_doc = None
        try:
            proof_doc = store_document(
                proof_file,
                str(adjustment_id),
                str(adjustment_id),
                session.get('user_id'),
                session.get('role'),
                'Stock Adjustment Proof',
            )
            if not proof_doc:
                raise ValueError('Supporting proof could not be saved.')

            result = create_stock_adjustment(
                entity['_id'],
                session.get('user_id'),
                lot_id=request.form.get('lot_id'),
                adjustment_type=request.form.get('adjustment_type'),
                quantity=request.form.get('quantity'),
                reason_code=request.form.get('reason_code'),
                reason=request.form.get('reason'),
                proof_filename=proof_doc.get('filename'),
                proof_document_id=proof_doc.get('_id'),
                adjustment_id=adjustment_id,
            )
            adjustment = result.get('adjustment') or {}
            log_action(
                session.get('user_id'),
                'submit_stock_adjustment',
                'stock_adjustment',
                str(adjustment.get('_id') or adjustment_id),
                metadata={
                    'adjustment_number': adjustment.get('adjustment_number'),
                    'product_name': adjustment.get('product_name'),
                    'adjustment_type': adjustment.get('adjustment_type'),
                    'quantity': adjustment.get('quantity_display'),
                },
            )
            flash(result.get('message') or 'Stock adjustment submitted for approval.', 'success')
            return redirect(url_for('admin.stock_adjustments'))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if proof_doc and proof_doc.get('_id'):
                mongo.db.documents.update_one(
                    {'_id': proof_doc['_id']},
                    {'$set': {'status': 'orphaned', 'updated_at': now_utc()}},
                )
            flash(str(exc), 'danger')
            return redirect(url_for('admin.stock_adjustments'))

    try:
        overview = get_stock_adjustment_overview(
            entity['_id'],
            session.get('user_id'),
            status_filter=request.args.get('status', 'all'),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [], 'lots': [], 'adjustment_types': {}, 'reason_codes': {},
            'selected_status': 'all', 'counts': {'submitted': 0, 'approved': 0, 'rejected': 0},
            'actor_role': session.get('role'),
        }
    return render_template('admin/stock_adjustments.html', overview=overview)


@admin_bp.route('/inventory/adjustments/<adjustment_id>/approve', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def approve_stock_adjustment_view(adjustment_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        result = approve_stock_adjustment(
            entity['_id'], session.get('user_id'), adjustment_id
        )
        adjustment = result.get('adjustment') or {}
        log_action(
            session.get('user_id'),
            'approve_stock_adjustment',
            'stock_adjustment',
            adjustment_id,
            metadata={
                'adjustment_number': adjustment.get('adjustment_number'),
                'product_name': adjustment.get('product_name'),
                'quantity': adjustment.get('quantity_display'),
            },
        )
        flash(result.get('message') or 'Stock adjustment approved.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.stock_adjustments'))


@admin_bp.route('/inventory/adjustments/<adjustment_id>/reject', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def reject_stock_adjustment_view(adjustment_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        result = reject_stock_adjustment(
            entity['_id'],
            session.get('user_id'),
            adjustment_id,
            request.form.get('rejection_reason'),
        )
        adjustment = result.get('adjustment') or {}
        log_action(
            session.get('user_id'),
            'reject_stock_adjustment',
            'stock_adjustment',
            adjustment_id,
            metadata={
                'adjustment_number': adjustment.get('adjustment_number'),
                'reason': adjustment.get('rejection_reason'),
            },
        )
        flash(result.get('message') or 'Stock adjustment rejected.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.stock_adjustments'))


@admin_bp.route('/products/ufc-marketplace', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def update_ufc_marketplace_publication():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    action = str(request.form.get('action') or 'publish').strip().lower()
    publish = action != 'unpublish'
    try:
        result = publish_products_to_ufc(
            entity['_id'],
            session.get('user_id'),
            request.form.getlist('product_ids'),
            publish=publish,
        )
        log_action(
            session.get('user_id'),
            'publish_products_to_ufc' if publish else 'unpublish_products_from_ufc',
            'product_marketplace',
            'bulk',
            metadata={
                'changed': result.get('changed', 0),
                'skipped': len(result.get('skipped') or []),
            },
        )
        flash(result.get('message') or 'Marketplace visibility updated.', 'success')
        skipped = result.get('skipped') or []
        if skipped:
            sample = '; '.join(
                f"{row.get('product_name') or row.get('product_id')}: {row.get('reason')}"
                for row in skipped[:3]
            )
            suffix = f" (+{len(skipped) - 3} more)" if len(skipped) > 3 else ''
            flash(f"Skipped: {sample}{suffix}", 'warning')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.product_list'))

def _supplier_page_context(edit_supplier=None, form_data=None):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return None
    overview = get_supplier_master_overview(
        entity['_id'],
        session.get('user_id'),
    )
    return {
        'overview': overview,
        'suppliers': overview.get('rows') or [],
        'options': overview.get('options') or {},
        'form_defaults': overview.get('form_defaults') or {},
        'edit_supplier': edit_supplier,
        'form_data': form_data,
    }


@admin_bp.route('/suppliers', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def supplier_master():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    if request.method == 'POST':
        try:
            result = create_supplier_from_operational_master(
                entity['_id'],
                session.get('user_id'),
                request.form,
            )
            flash(result.get('message') or 'Supplier saved.', 'success')
            return redirect(url_for('admin.supplier_master'))
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _supplier_page_context(form_data=request.form)
            return render_template('admin/supplier_master.html', **context), 400

    context = _supplier_page_context()
    return render_template('admin/supplier_master.html', **context)


@admin_bp.route('/suppliers/<supplier_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def edit_supplier_master(supplier_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    overview = get_supplier_master_overview(entity['_id'], session.get('user_id'))
    supplier = next(
        (row for row in overview.get('rows') or [] if row.get('id') == supplier_id),
        None,
    )
    if not supplier:
        flash('Supplier not found.', 'danger')
        return redirect(url_for('admin.supplier_master'))

    if request.method == 'POST':
        try:
            result = update_supplier_from_operational_master(
                supplier_id,
                session.get('user_id'),
                request.form,
                request.form.get('expected_version'),
            )
            flash(result.get('message') or 'Supplier updated.', 'success')
            return redirect(url_for('admin.supplier_master'))
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _supplier_page_context(
                edit_supplier=supplier,
                form_data=request.form,
            )
            return render_template('admin/supplier_master.html', **context), 400

    context = _supplier_page_context(edit_supplier=supplier)
    return render_template('admin/supplier_master.html', **context)


def _supplier_action(service_function, supplier_id, success_redirect='admin.supplier_master', **kwargs):
    try:
        result = service_function(
            supplier_id,
            session.get('user_id'),
            request.form.get('expected_version'),
            **kwargs,
        )
        flash(result.get('message') or 'Supplier workflow updated.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for(success_redirect))


@admin_bp.route('/suppliers/<supplier_id>/submit', methods=['POST'])
@login_required
@roles_required('accounts')
def submit_supplier_master(supplier_id):
    return _supplier_action(submit_party_ledger, supplier_id)


@admin_bp.route('/suppliers/<supplier_id>/approve', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def approve_supplier_master(supplier_id):
    return _supplier_action(
        approve_party_ledger,
        supplier_id,
        approval_note=request.form.get('approval_note', ''),
    )


@admin_bp.route('/suppliers/<supplier_id>/return', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def return_supplier_master(supplier_id):
    return _supplier_action(
        return_party_ledger,
        supplier_id,
        return_reason=request.form.get('reason', ''),
    )


@admin_bp.route('/suppliers/<supplier_id>/withdraw', methods=['POST'])
@login_required
@roles_required('accounts')
def withdraw_supplier_master(supplier_id):
    return _supplier_action(
        withdraw_party_ledger,
        supplier_id,
        reason=request.form.get('reason', ''),
    )


@admin_bp.route('/suppliers/<supplier_id>/cancel', methods=['POST'])
@login_required
@roles_required('accounts')
def cancel_supplier_master(supplier_id):
    return _supplier_action(
        cancel_party_ledger,
        supplier_id,
        reason=request.form.get('reason', ''),
    )


@admin_bp.route('/suppliers/<supplier_id>/deactivate', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def deactivate_supplier_master(supplier_id):
    return _supplier_action(
        deactivate_party_ledger,
        supplier_id,
        reason=request.form.get('reason', ''),
    )


@admin_bp.route('/suppliers/<supplier_id>/reactivate', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def reactivate_supplier_master(supplier_id):
    return _supplier_action(
        reactivate_party_ledger,
        supplier_id,
        reason=request.form.get('reason', ''),
    )


def _purchase_order_form_context(order=None, form_data=None):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return None
    catalog = get_purchase_order_form_catalog(
        entity['_id'],
        session.get('user_id'),
    )
    return {
        'catalog': catalog,
        'suppliers': catalog.get('suppliers') or [],
        'products': catalog.get('products') or [],
        'order': order,
        'form_data': form_data,
    }


@admin_bp.route('/purchase-orders')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def purchase_orders():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    overview = get_purchase_order_overview(
        entity['_id'],
        session.get('user_id'),
        status=request.args.get('status', '').strip(),
        query_text=request.args.get('q', '').strip(),
    )
    return render_template('admin/purchase_orders.html', overview=overview)


@admin_bp.route('/purchase-orders/create', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def create_purchase_order_view():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    if request.method == 'POST':
        try:
            result = create_purchase_order(
                entity['_id'],
                session.get('user_id'),
                {
                    **request.form.to_dict(),
                    'items_json': request.form.get('items_json', '[]'),
                },
                auto_approve=(
                    (
                        workflow_is_streamlined('avpl.purchase_order')
                        and request.form.get('save_action') != 'draft'
                    )
                    or (
                        not workflow_is_streamlined('avpl.purchase_order')
                        and request.form.get('save_action') == 'approve'
                        and session.get('role') in ['avpl_admin', 'super_admin']
                    )
                ),
            )
            flash(result.get('message') or 'Purchase order saved.', 'success')
            return redirect(
                url_for(
                    'admin.purchase_order_detail',
                    order_id=result['order']['id'],
                )
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _purchase_order_form_context(form_data=request.form)
            return render_template('admin/purchase_order_form.html', **context), 400

    context = _purchase_order_form_context()
    return render_template('admin/purchase_order_form.html', **context)


@admin_bp.route('/purchase-orders/<order_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def purchase_order_detail(order_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        order = get_purchase_order(
            entity['_id'],
            session.get('user_id'),
            order_id,
        )
        goods_receipts = get_goods_receipts_for_purchase_order(
            entity['_id'],
            session.get('user_id'),
            order_id,
        )
        supplier_invoices = get_supplier_invoices_for_purchase_order(
            entity['_id'],
            session.get('user_id'),
            order_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.purchase_orders'))
    return render_template(
        'admin/purchase_order_detail.html',
        order=order,
        goods_receipts=goods_receipts,
        supplier_invoices=supplier_invoices,
    )


@admin_bp.route('/purchase-orders/<order_id>/print')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def print_purchase_order_view(order_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        order = get_purchase_order(
            entity['_id'],
            session.get('user_id'),
            order_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.purchase_orders'))

    return render_template(
        'admin/purchase_order_print.html',
        order=order,
        entity={
            'legal_name': entity.get('legal_name') or entity.get('name') or 'AVPL',
            'trade_name': entity.get('trade_name') or entity.get('display_name') or 'UnnatFarm',
            'address_line_1': entity.get('address_line_1') or entity.get('address') or entity.get('registered_address') or '',
            'address_line_2': entity.get('address_line_2') or '',
            'city': entity.get('city') or '',
            'district': entity.get('district') or '',
            'state': entity.get('state') or entity.get('state_name') or '',
            'postal_code': entity.get('postal_code') or '',
            'gstin': entity.get('gstin') or '',
            'pan': entity.get('pan') or '',
        },
    )


@admin_bp.route('/purchase-orders/<order_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def edit_purchase_order_view(order_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        order = get_purchase_order(
            entity['_id'],
            session.get('user_id'),
            order_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.purchase_orders'))

    if request.method == 'POST':
        try:
            result = update_purchase_order(
                order_id,
                session.get('user_id'),
                {
                    **request.form.to_dict(),
                    'items_json': request.form.get('items_json', '[]'),
                },
                request.form.get('expected_version'),
                auto_approve=(
                    (
                        workflow_is_streamlined('avpl.purchase_order')
                        and request.form.get('save_action') != 'draft'
                    )
                    or (
                        not workflow_is_streamlined('avpl.purchase_order')
                        and request.form.get('save_action') == 'approve'
                        and session.get('role') in ['avpl_admin', 'super_admin']
                    )
                ),
            )
            flash(result.get('message') or 'Purchase order updated.', 'success')
            return redirect(url_for('admin.purchase_order_detail', order_id=order_id))
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _purchase_order_form_context(
                order=order,
                form_data=request.form,
            )
            return render_template('admin/purchase_order_form.html', **context), 400

    context = _purchase_order_form_context(order=order)
    return render_template('admin/purchase_order_form.html', **context)


def _purchase_order_action(service_function, order_id, **kwargs):
    try:
        result = service_function(
            order_id,
            session.get('user_id'),
            request.form.get('expected_version'),
            **kwargs,
        )
        flash(result.get('message') or 'Purchase-order workflow updated.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.purchase_order_detail', order_id=order_id))


@admin_bp.route('/purchase-orders/<order_id>/complete', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def complete_purchase_order_view(order_id):
    """One-click completion for a saved routine PO in streamlined mode."""
    if not workflow_is_streamlined('avpl.purchase_order'):
        flash('Simplified purchase-order completion is disabled.', 'warning')
        return redirect(url_for('admin.purchase_order_detail', order_id=order_id))
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        order = get_purchase_order(entity['_id'], session.get('user_id'), order_id)
        if order.get('status') in ['draft', 'returned_for_correction']:
            submitted = submit_purchase_order(order_id, session.get('user_id'), order.get('version'))
            order = submitted.get('order') or order
        if order.get('status') == 'pending_approval':
            completed = approve_purchase_order(
                order_id, session.get('user_id'), order.get('version'),
                approval_note='Completed through streamlined purchase workflow.',
            )
            flash(completed.get('message') or 'Purchase order completed.', 'success')
        else:
            flash('Purchase order is already ready for fulfilment.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.purchase_order_detail', order_id=order_id))


@admin_bp.route('/purchase-orders/<order_id>/submit', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def submit_purchase_order_view(order_id):
    return _purchase_order_action(submit_purchase_order, order_id)


@admin_bp.route('/purchase-orders/<order_id>/approve', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def approve_purchase_order_view(order_id):
    return _purchase_order_action(
        approve_purchase_order,
        order_id,
        approval_note=request.form.get('approval_note', ''),
    )


@admin_bp.route('/purchase-orders/<order_id>/return', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def return_purchase_order_view(order_id):
    return _purchase_order_action(
        return_purchase_order,
        order_id,
        reason=request.form.get('reason', ''),
    )


@admin_bp.route('/purchase-orders/<order_id>/withdraw', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def withdraw_purchase_order_view(order_id):
    return _purchase_order_action(
        withdraw_purchase_order,
        order_id,
        reason=request.form.get('reason', ''),
    )


@admin_bp.route('/purchase-orders/<order_id>/cancel', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def cancel_purchase_order_view(order_id):
    return _purchase_order_action(
        cancel_purchase_order,
        order_id,
        reason=request.form.get('reason', ''),
    )




# ---------------------------------------------------------------------------
# Stage 2 · Batch 2.3 — Goods Receipt Notes
# ---------------------------------------------------------------------------


def _goods_receipt_form_context(receipt=None, form_data=None):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return None

    selected_po_id = ''
    if form_data:
        selected_po_id = form_data.get('purchase_order_id', '')
    elif receipt:
        selected_po_id = receipt.get('purchase_order_id', '')
    else:
        selected_po_id = request.args.get('po_id', '').strip()

    catalog = get_goods_receipt_form_catalog(
        entity['_id'],
        session.get('user_id'),
        selected_po_id,
    )
    return {
        'catalog': catalog,
        'purchase_orders': catalog.get('purchase_orders') or [],
        'receipt': receipt,
        'form_data': form_data,
        'selected_purchase_order_id': selected_po_id,
    }


def _save_grn_supplier_invoice_attachment(receipt_id):
    file_storage = request.files.get('supplier_invoice_file')
    if not file_storage or not file_storage.filename:
        return None
    document = store_document(
        file_storage,
        str(receipt_id),
        str(receipt_id),
        session.get('user_id'),
        session.get('role'),
        'Supplier Invoice Attachment',
    )
    if not document:
        raise ValueError('The supplier invoice attachment could not be saved.')
    return attach_goods_receipt_supplier_invoice_document(
        receipt_id,
        session.get('user_id'),
        document.get('_id'),
        document.get('filename'),
        supplier_invoice_number=request.form.get('supplier_invoice_number_capture', ''),
        supplier_invoice_date=request.form.get('supplier_invoice_date_capture', ''),
    )


@admin_bp.route('/goods-receipts')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def goods_receipts():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    overview = get_goods_receipt_overview(
        entity['_id'],
        session.get('user_id'),
        status=request.args.get('status', '').strip(),
        query_text=request.args.get('q', '').strip(),
    )
    return render_template(
        'admin/goods_receipts.html',
        overview=overview,
    )


@admin_bp.route('/goods-receipts/create', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def create_goods_receipt_view():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    if request.method == 'POST':
        try:
            result = create_goods_receipt(
                entity['_id'],
                session.get('user_id'),
                {
                    **request.form.to_dict(),
                    'items_json': request.form.get('items_json', '[]'),
                },
                auto_post=(
                    (
                        workflow_is_streamlined('avpl.goods_receipt')
                        and request.form.get('save_action') != 'draft'
                    )
                    or (
                        not workflow_is_streamlined('avpl.goods_receipt')
                        and request.form.get('save_action') == 'post'
                        and session.get('role') in ['avpl_admin', 'super_admin']
                    )
                ),
            )
            attachment_warning = None
            try:
                _save_grn_supplier_invoice_attachment(result['receipt']['id'])
            except (ValueError, RuntimeError) as attachment_exc:
                attachment_warning = str(attachment_exc)

            flash(
                result.get('message') or 'Goods Receipt saved.',
                'success',
            )
            if request.form.get('supplier_invoice_received') == 'on' and not request.files.get('supplier_invoice_file'):
                flash(
                    'Goods Receipt was saved. The supplier invoice reference is recorded, but no invoice file was attached; you can attach it from the Goods Receipt page.',
                    'warning',
                )
            elif attachment_warning:
                flash(
                    'Goods Receipt was saved, but the supplier invoice file needs attention: ' + attachment_warning,
                    'warning',
                )
            return redirect(
                url_for(
                    'admin.goods_receipt_detail',
                    receipt_id=result['receipt']['id'],
                )
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _goods_receipt_form_context(form_data=request.form)
            return render_template(
                'admin/goods_receipt_form.html',
                **context,
            ), 400

    context = _goods_receipt_form_context()
    return render_template('admin/goods_receipt_form.html', **context)


@admin_bp.route('/goods-receipts/<receipt_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def goods_receipt_detail(receipt_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        receipt = get_goods_receipt(
            entity['_id'],
            session.get('user_id'),
            receipt_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.goods_receipts'))
    related_purchase_invoices = get_supplier_invoices_for_purchase_order(
        entity['_id'],
        session.get('user_id'),
        receipt.get('purchase_order_id'),
    )
    invoice_catalog = get_supplier_invoice_form_catalog(
        entity['_id'],
        session.get('user_id'),
        receipt.get('purchase_order_id'),
        source_grn_id=receipt.get('id'),
    )
    linked_purchase_invoice = None
    linked_id = str(receipt.get('supplier_invoice_record_id') or '')
    if linked_id:
        linked_purchase_invoice = next(
            (row for row in related_purchase_invoices if str(row.get('id')) == linked_id),
            None,
        )
    if not linked_purchase_invoice and receipt.get('supplier_invoice_number_capture'):
        linked_purchase_invoice = next(
            (
                row
                for row in related_purchase_invoices
                if str(row.get('supplier_invoice_number') or '').strip().lower()
                == str(receipt.get('supplier_invoice_number_capture') or '').strip().lower()
            ),
            None,
        )
    can_record_purchase_invoice = bool(invoice_catalog.get('purchase_orders'))
    if not linked_purchase_invoice and not can_record_purchase_invoice and len(related_purchase_invoices) == 1:
        linked_purchase_invoice = related_purchase_invoices[0]

    return render_template(
        'admin/goods_receipt_detail.html',
        receipt=receipt,
        linked_purchase_invoice=linked_purchase_invoice,
        related_purchase_invoices=related_purchase_invoices,
        can_record_purchase_invoice=can_record_purchase_invoice,
    )


@admin_bp.route('/goods-receipts/<receipt_id>/supplier-invoice-attachment', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def goods_receipt_supplier_invoice_attachment(receipt_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        receipt = get_goods_receipt(
            entity['_id'],
            session.get('user_id'),
            receipt_id,
        )
        if not request.files.get('supplier_invoice_file') or not request.files['supplier_invoice_file'].filename:
            raise ValueError('Choose the supplier GST/tax invoice file to upload.')
        result = _save_grn_supplier_invoice_attachment(receipt_id)
        flash((result or {}).get('message') or 'Supplier invoice attachment saved.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.goods_receipt_detail', receipt_id=receipt_id))


@admin_bp.route('/goods-receipts/<receipt_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def edit_goods_receipt_view(receipt_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        receipt = get_goods_receipt(
            entity['_id'],
            session.get('user_id'),
            receipt_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.goods_receipts'))

    if request.method == 'POST':
        try:
            result = update_goods_receipt(
                receipt_id,
                session.get('user_id'),
                {
                    **request.form.to_dict(),
                    'items_json': request.form.get('items_json', '[]'),
                },
                request.form.get('expected_version'),
                auto_post=(
                    (
                        workflow_is_streamlined('avpl.goods_receipt')
                        and request.form.get('save_action') != 'draft'
                    )
                    or (
                        not workflow_is_streamlined('avpl.goods_receipt')
                        and request.form.get('save_action') == 'post'
                        and session.get('role') in ['avpl_admin', 'super_admin']
                    )
                ),
            )
            attachment_warning = None
            try:
                _save_grn_supplier_invoice_attachment(receipt_id)
            except (ValueError, RuntimeError) as attachment_exc:
                attachment_warning = str(attachment_exc)
            flash(
                result.get('message') or 'Goods Receipt updated.',
                'success',
            )
            if attachment_warning:
                flash(
                    'Goods Receipt was updated, but the supplier invoice file needs attention: ' + attachment_warning,
                    'warning',
                )
            return redirect(
                url_for(
                    'admin.goods_receipt_detail',
                    receipt_id=receipt_id,
                )
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _goods_receipt_form_context(
                receipt=receipt,
                form_data=request.form,
            )
            return render_template(
                'admin/goods_receipt_form.html',
                **context,
            ), 400

    context = _goods_receipt_form_context(receipt=receipt)
    return render_template('admin/goods_receipt_form.html', **context)


def _goods_receipt_action(service_function, receipt_id, **kwargs):
    try:
        result = service_function(
            receipt_id,
            session.get('user_id'),
            request.form.get('expected_version'),
            **kwargs,
        )
        flash(
            result.get('message') or 'Goods Receipt workflow updated.',
            'success',
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(
        url_for('admin.goods_receipt_detail', receipt_id=receipt_id)
    )


@admin_bp.route('/goods-receipts/<receipt_id>/submit', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def submit_goods_receipt_view(receipt_id):
    return _goods_receipt_action(submit_goods_receipt, receipt_id)


@admin_bp.route('/goods-receipts/<receipt_id>/post', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def post_goods_receipt_view(receipt_id):
    return _goods_receipt_action(
        post_goods_receipt,
        receipt_id,
        posting_note=request.form.get('posting_note', ''),
        allow_creator_post=workflow_is_streamlined('avpl.goods_receipt'),
    )


@admin_bp.route('/goods-receipts/<receipt_id>/return', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def return_goods_receipt_view(receipt_id):
    return _goods_receipt_action(
        return_goods_receipt,
        receipt_id,
        reason=request.form.get('reason', ''),
    )


@admin_bp.route('/goods-receipts/<receipt_id>/cancel', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def cancel_goods_receipt_view(receipt_id):
    return _goods_receipt_action(
        cancel_goods_receipt,
        receipt_id,
        reason=request.form.get('reason', ''),
    )


# ---------------------------------------------------------------------------
# Stage 2 · Batch 2.4 — Supplier Invoice and Three-Way Match
# ---------------------------------------------------------------------------


def _supplier_invoice_form_context(invoice=None, form_data=None):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return None

    selected_po_id = ''
    if form_data:
        selected_po_id = form_data.get('purchase_order_id', '')
    elif invoice:
        selected_po_id = invoice.get('purchase_order_id', '')
    else:
        selected_po_id = request.args.get('po_id', '').strip()

    selected_source_grn_id = ''
    if form_data:
        selected_source_grn_id = form_data.get('source_grn_id', '')
    elif invoice:
        selected_source_grn_id = invoice.get('source_grn_id', '')
    else:
        selected_source_grn_id = request.args.get('grn_id', '').strip()

    catalog = get_supplier_invoice_form_catalog(
        entity['_id'],
        session.get('user_id'),
        selected_po_id,
        source_grn_id=selected_source_grn_id,
        include_fully_invoiced=bool(invoice),
    )
    return {
        'catalog': catalog,
        'purchase_orders': catalog.get('purchase_orders') or [],
        'invoice': invoice,
        'form_data': form_data,
        'selected_purchase_order_id': selected_po_id,
        'selected_source_grn_id': selected_source_grn_id,
    }


@admin_bp.route('/supplier-invoices')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def supplier_invoices():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    overview = get_supplier_invoice_overview(
        entity['_id'],
        session.get('user_id'),
        status=request.args.get('status', '').strip(),
        query_text=request.args.get('q', '').strip(),
    )
    return render_template(
        'admin/supplier_invoices.html',
        overview=overview,
    )


def _auto_finalize_supplier_invoice_if_ready(result):
    """Finalize a matched supplier invoice in one save when streamlined mode is enabled.

    Matching remains the safety gate. If final posting fails for a configuration reason, the
    recorded invoice is preserved and the detail page exposes a safe retry action.
    """
    if not workflow_is_streamlined('avpl.supplier_invoice_posting'):
        return result, None
    invoice = (result or {}).get('invoice') or {}
    if invoice.get('status') not in ['matched', 'matched_with_warnings']:
        return result, None
    if int(invoice.get('blocking_mismatch_count') or 0) > 0:
        return result, None
    if invoice.get('posting_status') == 'posted':
        return result, None
    try:
        posted = post_supplier_invoice_purchase(
            invoice.get('id'),
            session.get('user_id'),
            invoice.get('version'),
        )
        return posted, None
    except (ValueError, PermissionError, RuntimeError) as exc:
        return result, str(exc)


@admin_bp.route('/supplier-invoices/create', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def create_supplier_invoice_view():
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    if request.method == 'POST':
        try:
            result = create_supplier_invoice(
                entity['_id'],
                session.get('user_id'),
                {
                    **request.form.to_dict(),
                    'items_json': request.form.get('items_json', '[]'),
                },
            )
            result, finalize_warning = _auto_finalize_supplier_invoice_if_ready(result)
            category = (
                'success'
                if result['invoice']['status'] in [
                    'matched',
                    'matched_with_warnings',
                ]
                else 'warning'
            )
            flash(
                result.get('message') or 'Supplier Invoice recorded.',
                category,
            )
            if finalize_warning:
                flash(
                    'Invoice was recorded, but automatic finalization needs attention: ' + finalize_warning,
                    'warning',
                )
            return redirect(
                url_for(
                    'admin.supplier_invoice_detail',
                    invoice_id=result['invoice']['id'],
                )
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _supplier_invoice_form_context(
                form_data=request.form,
            )
            return render_template(
                'admin/supplier_invoice_form.html',
                **context,
            ), 400

    context = _supplier_invoice_form_context()
    return render_template('admin/supplier_invoice_form.html', **context)


@admin_bp.route('/supplier-invoices/<invoice_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def supplier_invoice_detail(invoice_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        invoice = get_supplier_invoice(
            entity['_id'],
            session.get('user_id'),
            invoice_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.supplier_invoices'))
    return render_template(
        'admin/supplier_invoice_detail.html',
        invoice=invoice,
    )


@admin_bp.route('/supplier-invoices/<invoice_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def edit_supplier_invoice_view(invoice_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))
    try:
        invoice = get_supplier_invoice(
            entity['_id'],
            session.get('user_id'),
            invoice_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.supplier_invoices'))

    if request.method == 'POST':
        try:
            result = update_supplier_invoice(
                invoice_id,
                session.get('user_id'),
                {
                    **request.form.to_dict(),
                    'items_json': request.form.get('items_json', '[]'),
                },
                request.form.get('expected_version'),
            )
            result, finalize_warning = _auto_finalize_supplier_invoice_if_ready(result)
            category = (
                'success'
                if result['invoice']['status'] in [
                    'matched',
                    'matched_with_warnings',
                ]
                else 'warning'
            )
            flash(
                result.get('message') or 'Supplier Invoice updated.',
                category,
            )
            return redirect(
                url_for(
                    'admin.supplier_invoice_detail',
                    invoice_id=invoice_id,
                )
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
            context = _supplier_invoice_form_context(
                invoice=invoice,
                form_data=request.form,
            )
            return render_template(
                'admin/supplier_invoice_form.html',
                **context,
            ), 400

    context = _supplier_invoice_form_context(invoice=invoice)
    return render_template('admin/supplier_invoice_form.html', **context)


@admin_bp.route('/supplier-invoices/<invoice_id>/cancel', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def cancel_supplier_invoice_view(invoice_id):
    try:
        result = cancel_supplier_invoice(
            invoice_id,
            session.get('user_id'),
            request.form.get('expected_version'),
            request.form.get('reason', ''),
        )
        flash(
            result.get('message') or 'Supplier Invoice cancelled.',
            'success',
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(
        url_for('admin.supplier_invoice_detail', invoice_id=invoice_id)
    )


# ---------------------------------------------------------------------------
# Stage 2 · Batch 2.5 — Purchase Posting, Supplier Payable and Print
# ---------------------------------------------------------------------------


@admin_bp.route(
    '/supplier-invoices/<invoice_id>/prepare-posting',
    methods=['POST'],
)
@login_required
@roles_required('super_admin', 'accounts')
def prepare_supplier_invoice_posting_view(invoice_id):
    try:
        result = prepare_supplier_invoice_posting(
            invoice_id,
            session.get('user_id'),
            request.form.get('expected_version'),
        )
        flash(
            result.get('message')
            or 'Purchase posting prepared successfully.',
            'success',
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')

    return redirect(
        url_for('admin.supplier_invoice_detail', invoice_id=invoice_id)
    )


@admin_bp.route(
    '/supplier-invoices/<invoice_id>/post-purchase',
    methods=['POST'],
)
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def post_supplier_invoice_purchase_view(invoice_id):
    try:
        result = post_supplier_invoice_purchase(
            invoice_id,
            session.get('user_id'),
            request.form.get('expected_version'),
        )
        flash(
            result.get('message')
            or 'Purchase Invoice posted successfully.',
            'success',
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')

    return redirect(
        url_for('admin.supplier_invoice_detail', invoice_id=invoice_id)
    )


@admin_bp.route('/supplier-invoices/<invoice_id>/print')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def print_supplier_invoice_view(invoice_id):
    entity = _stage2_entity_or_redirect()
    if not entity:
        return redirect(url_for('accounting.dashboard'))

    try:
        context = get_purchase_invoice_print_context(
            entity['_id'],
            session.get('user_id'),
            invoice_id,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(
            url_for('admin.supplier_invoice_detail', invoice_id=invoice_id)
        )

    return render_template(
        'admin/supplier_invoice_print.html',
        **context,
    )


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

# ---------------------------------------------------------------------------
# Stage 4 — AVPL -> UFC Order Requests, Reservation and Dispatch
# ---------------------------------------------------------------------------


@admin_bp.route('/ufc-orders')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def ufc_order_requests():
    try:
        overview = get_avpl_order_overview(
            session.get('user_id'),
            status_filter=request.args.get('status', 'all'),
            search=request.args.get('q', ''),
            page=request.args.get('page', 1, type=int) or 1,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'selected_status': request.args.get('status', 'all'),
            'query': request.args.get('q', ''),
            'statuses': {},
            'counts': {},
            'pagination': {
                'page': 1,
                'total': 0,
                'total_pages': 1,
                'has_prev': False,
                'has_next': False,
            },
        }
    return render_template('admin/ufc_order_requests.html', overview=overview)


@admin_bp.route('/ufc-orders/<order_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def ufc_order_request_detail(order_id):
    try:
        # Read access is already protected by route roles. The service detail
        # keeps this page independent from legacy `orders` records.
        order = get_avpl_ufc_order(order_id)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.ufc_order_requests'))
    return render_template('admin/ufc_order_request_detail.html', order=order)


@admin_bp.route('/ufc-orders/<order_id>/approve', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def approve_ufc_order_view(order_id):
    try:
        result = approve_ufc_order(
            session.get('user_id'),
            order_id,
            request.form.get('approved_quantity'),
            request.form.get('unit_price'),
            note=request.form.get('approval_note', ''),
            credit_period_days=request.form.get('credit_period_days', 0),
        )
        order = result.get('order') or {}
        log_action(
            session.get('user_id'),
            'approve_ufc_order',
            'avpl_ufc_order',
            order_id,
            metadata={
                'order_number': order.get('order_number'),
                'centre_uid': order.get('centre_uid'),
                'product_name': order.get('product_name'),
                'approved_quantity': order.get('approved_quantity_display'),
            },
        )
        flash(result.get('message') or 'UFC order approved.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.ufc_order_request_detail', order_id=order_id))


@admin_bp.route('/ufc-orders/<order_id>/reject', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def reject_ufc_order_view(order_id):
    try:
        result = reject_ufc_order(
            session.get('user_id'),
            order_id,
            reason=request.form.get('reason', ''),
        )
        order = result.get('order') or {}
        log_action(
            session.get('user_id'),
            'reject_ufc_order',
            'avpl_ufc_order',
            order_id,
            metadata={
                'order_number': order.get('order_number'),
                'centre_uid': order.get('centre_uid'),
                'product_name': order.get('product_name'),
            },
        )
        flash(result.get('message') or 'UFC order rejected.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.ufc_order_request_detail', order_id=order_id))


@admin_bp.route('/ufc-orders/<order_id>/cancel', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def cancel_ufc_order_view(order_id):
    try:
        result = cancel_approved_ufc_order(
            session.get('user_id'),
            order_id,
            reason=request.form.get('reason', ''),
        )
        order = result.get('order') or {}
        log_action(
            session.get('user_id'),
            'cancel_ufc_order',
            'avpl_ufc_order',
            order_id,
            metadata={
                'order_number': order.get('order_number'),
                'centre_uid': order.get('centre_uid'),
                'product_name': order.get('product_name'),
            },
        )
        flash(result.get('message') or 'Order cancelled and reservation released.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.ufc_order_request_detail', order_id=order_id))


@admin_bp.route('/ufc-orders/<order_id>/dispatch', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def dispatch_ufc_order_view(order_id):
    try:
        result = dispatch_ufc_order(
            session.get('user_id'),
            order_id,
            dispatch_note=request.form.get('dispatch_note', ''),
            transporter=request.form.get('transporter_name', ''),
            vehicle_number=request.form.get('vehicle_number', ''),
        )
        order = result.get('order') or {}
        log_action(
            session.get('user_id'),
            'dispatch_ufc_order',
            'avpl_ufc_order',
            order_id,
            metadata={
                'order_number': order.get('order_number'),
                'centre_uid': order.get('centre_uid'),
                'product_name': order.get('product_name'),
                'dispatched_quantity': order.get('dispatched_quantity_display'),
            },
        )
        flash(result.get('message') or 'UFC order dispatched.', 'success')
        if result.get('financial_warning'):
            flash(
                'Dispatch is complete, but the Sales Invoice needs financial-sync review: ' + str(result.get('financial_warning')),
                'warning',
            )
        elif result.get('financial'):
            invoice = (result.get('financial') or {}).get('invoice') or {}
            if invoice.get('invoice_number'):
                flash(f"Sales Invoice {invoice.get('invoice_number')} generated automatically.", 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.ufc_order_request_detail', order_id=order_id))


# ---------------------------------------------------------------------------
# Stage 5 — AVPL Sales, Sales Invoice and UFC Financial Link
# ---------------------------------------------------------------------------


@admin_bp.route('/ufc-sales')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def ufc_sales():
    try:
        overview = get_avpl_sales_overview(
            session.get('user_id'),
            search=request.args.get('q', ''),
            payment_status=request.args.get('payment', 'all'),
            page=request.args.get('page', 1, type=int) or 1,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'query': request.args.get('q', ''),
            'selected_payment': request.args.get('payment', 'all'),
            'payment_statuses': {},
            'summary': {'sale_count': 0, 'invoice_count': 0, 'total_sales': '0.00', 'outstanding': '0.00'},
            'pagination': {'page': 1, 'total': 0, 'total_pages': 1, 'has_prev': False, 'has_next': False},
        }
    return render_template('admin/ufc_sales.html', overview=overview)


@admin_bp.route('/ufc-sales/<sale_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def ufc_sale_detail(sale_id):
    try:
        sale = get_avpl_sale(session.get('user_id'), sale_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.ufc_sales'))
    return render_template('admin/ufc_sale_detail.html', sale=sale)


@admin_bp.route('/ufc-sales-invoices/<invoice_id>/print')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def ufc_sales_invoice_print(invoice_id):
    try:
        context = get_sales_invoice_print_context(
            invoice_id,
            actor_user_id=session.get('user_id'),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.ufc_sales'))
    return render_template('admin/ufc_sales_invoice_print.html', **context, viewer='avpl')


@admin_bp.route('/ufc-orders/<order_id>/financial-sync', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def sync_ufc_order_financials(order_id):
    try:
        result = ensure_sales_documents_for_order(session.get('user_id'), order_id)
        invoice = result.get('invoice') or {}
        flash(
            f"Financial documents synchronized. Sales Invoice {invoice.get('invoice_number') or ''} is ready.",
            'success',
        )
        log_action(
            session.get('user_id'),
            'sync_ufc_order_sales_financials',
            'avpl_ufc_order',
            order_id,
            metadata={'invoice_number': invoice.get('invoice_number') or ''},
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.ufc_order_request_detail', order_id=order_id))


@admin_bp.route('/ufc-sales/sync-existing', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin')
def sync_existing_ufc_sales():
    try:
        result = bulk_sync_existing_orders(session.get('user_id'), limit=100)
        if result.get('failed'):
            flash(
                f"Sales sync completed: {result.get('synced', 0)} created/repaired, {result.get('failed', 0)} need review.",
                'warning',
            )
        else:
            flash(f"Sales sync completed successfully for {result.get('synced', 0)} order(s).", 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.ufc_sales'))

# ---------------------------------------------------------------------------
# Stage 8 — AVPL unified payments, settlement and accounting event queue
# ---------------------------------------------------------------------------


@admin_bp.route('/payments')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def payments_dashboard():
    try:
        overview = get_avpl_payment_overview(session.get('user_id'))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'supplier_payables': [],
            'ufc_receivables': [],
            'pending_ufc_payments': [],
            'farmer_payables': [],
            'recent_payments': [],
            'payment_modes': {},
            'summary': {
                'supplier_due': '0.00',
                'ufc_due': '0.00',
                'farmer_due': '0.00',
                'ufc_pending_confirmation': '0.00',
                'ufc_pending_count': 0,
                'recent_count': 0,
                'accounting_pending': 0,
            },
        }
    return render_template('admin/payments.html', overview=overview)


@admin_bp.route('/payments/record', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def record_payment_view():
    try:
        result = stage8_record_payment(
            session.get('user_id'),
            request.form.get('source_type'),
            request.form.get('invoice_id'),
            request.form.get('amount'),
            request.form.get('payment_mode'),
            reference=request.form.get('reference', ''),
            note=request.form.get('note', ''),
            idempotency_key=request.form.get('payment_token', ''),
        )
        flash(result.get('message') or 'Payment recorded.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.payments_dashboard'))


@admin_bp.route('/payments/reported/<payment_id>/confirm', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def confirm_ufc_payment_report(payment_id):
    try:
        result = stage8_confirm_reported_payment(session.get('user_id'), payment_id)
        flash(result.get('message') or 'UFC payment confirmed.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.payments_dashboard'))


@admin_bp.route('/payments/reported/<payment_id>/return', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def reject_ufc_payment_report(payment_id):
    try:
        result = stage8_reject_reported_payment(
            session.get('user_id'),
            payment_id,
            request.form.get('reason', ''),
        )
        flash(result.get('message') or 'UFC payment report returned.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.payments_dashboard'))


@admin_bp.route('/payments/<payment_id>/reverse', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def reverse_payment_view(payment_id):
    try:
        result = stage8_reverse_payment(
            session.get('user_id'), payment_id, request.form.get('reason', '')
        )
        flash(result.get('message') or 'Payment reversed.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('admin.payments_dashboard'))


@admin_bp.route('/payment-receipts/<payment_id>/print')
@login_required
@roles_required('super_admin', 'avpl_admin', 'accounts')
def payment_receipt_print(payment_id):
    try:
        context = get_payment_receipt_context(session.get('user_id'), payment_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin.payments_dashboard'))
    return render_template('modules/payment_receipt_print.html', **context)

