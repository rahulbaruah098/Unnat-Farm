# UnnatFarm Requirement Coverage Matrix

This file maps the requested scope to the included codebase.

## Core architecture
- Flask + MongoDB modular architecture: `app/__init__.py`, `app/blueprints/*`, `app/services/*`
- Single login: `app/blueprints/auth/routes.py`
- Strict RBAC: `app/utils/decorators.py`
- Separate auth vs master data: `users`, `farmer_master`, `ufc_admin_master`, `ufc_mitra_master`

## Non-negotiable mapping
- UFC Admin system-generated centre UID: `app/services/uid_service.py`, `app/services/user_service.py`
- UFC Mitra system-generated mitra UID: `app/services/uid_service.py`, `app/services/user_service.py`
- Farmer registration with exact requested fields: `templates/auth/register_farmer.html`
- Centre UID + Mitra UID validation: `app/services/mapping_service.py`
- Mitra belongs to centre validation: `app/services/mapping_service.py`

## Approval workflows
- UFC Admin validated by AVPL: `app/blueprints/validations/routes.py`
- UFC Mitra validated by AVPL: `app/blueprints/validations/routes.py`
- Farmer validated by UFC Mitra: `app/blueprints/validations/routes.py`
- Restricted access before approval: `app/utils/decorators.py`, dashboard routing logic

## Document visibility
- Upload model and metadata: `app/services/document_service.py`
- AVPL preview/download: `documents` blueprint + validation detail page

## Dashboards and role pages
- Super Admin: `templates/dashboard/super_admin.html`
- AVPL Admin: `templates/dashboard/avpl_admin.html`
- Accounts: `templates/dashboard/accounts.html`
- Sales NeLocals: `templates/dashboard/sales_nelocals.html`
- Sales UnnatFarm: `templates/dashboard/sales_unnatfarm.html`
- UFC Admin: `templates/dashboard/ufc_admin.html`
- UFC Mitra: `templates/dashboard/ufc_mitra.html`
- Farmer: `templates/dashboard/farmer.html`

## Modules
- Products, Orders, Buy, Sell, Finance, Insurance, LMS, Support, Transactions, POS placeholders
- Each has route coverage in `app/blueprints/modules/routes.py`
- Each has page templates in `app/templates/modules/`

## Admin capabilities
- Create IDs: `app/blueprints/admin/routes.py`
- User listing and deletion: `app/blueprints/admin/routes.py`
- LMS upload: `app/blueprints/admin/routes.py`
- Product addition: `app/blueprints/admin/routes.py`

## Master data visibility
- Farmers: `master_data` blueprint
- UFC Admins: `master_data` blueprint
- UFC Mitras: `master_data` blueprint

## Future-ready mobile note
- Architecture docs: `docs/architecture_notes.md`
- Collections / routes / services kept modular for future API split
