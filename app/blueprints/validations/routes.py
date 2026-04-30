from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.services.validation_service import list_validations_for_role, action_validation, can_act_on_validation

validation_bp = Blueprint('validation', __name__, url_prefix='/validations')


@validation_bp.route('/')
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_mitra')
def queue():
    q = request.args.get("q", "").strip()

    items = list_validations_for_role(session['role'], session)

    if q:
        q_lower = q.lower()
        items = [
            item for item in items
            if q_lower in str(item.get("entity_type", "")).lower()
            or q_lower in str(item.get("status", "")).lower()
            or q_lower in str(item.get("created_at", "")).lower()
        ]

    return render_template('validations/queue.html', items=items)


@validation_bp.route('/<validation_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_mitra')
def detail(validation_id):
    item = mongo.db.validations.find_one({'_id': ObjectId(validation_id)})
    if not item:
        abort(404)
    if not can_act_on_validation(item, session.get("role"), session):
        abort(403)
    entity = mongo.db.users.find_one({'_id': ObjectId(item['entity_id'])})
    master = None
    if item['entity_type'] == 'ufc_admin_profile':
        master = mongo.db.ufc_admin_master.find_one({'linked_user_id': item['entity_id']})
    elif item['entity_type'] == 'ufc_mitra_profile':
        master = mongo.db.ufc_mitra_master.find_one({'linked_user_id': item['entity_id']})
    elif item['entity_type'] == 'farmer_registration':
        master = mongo.db.farmer_master.find_one({'linked_user_id': item['entity_id']})
    master_id = str(master.get('_id')) if master else item.get('metadata', {}).get('master_id')
    linked_docs = list(mongo.db.documents.find({'$or': [{'linked_user_id': item['entity_id']}, {'linked_master_id': master_id}]}).sort('created_at', -1))
    return render_template('validations/detail.html', item=item, entity=entity, master=master, linked_docs=linked_docs)


@validation_bp.route('/<validation_id>/action', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_mitra')
def take_action(validation_id):
    action = request.form.get('action')
    remarks = request.form.get('remarks', '')
    item = mongo.db.validations.find_one({"_id": ObjectId(validation_id)})
    if not can_act_on_validation(item, session.get("role"), session):
        abort(403)
    ok, message = action_validation(validation_id, session['user_id'], action, remarks)
    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('validation.queue'))
