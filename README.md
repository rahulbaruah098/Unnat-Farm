# UnnatFarm MIS - Expanded MVP Project

A fuller production-oriented Flask + MongoDB codebase for the UnnatFarm centralized web MIS.

## What this project includes
- Single common login for all roles
- Role-based redirection and access control
- Strict hierarchy-aware RBAC
- Separate `users` authentication collection and dedicated master-data collections
- UFC Admin centre UID generation
- UFC Mitra UID generation
- Farmer registration with exact requested fields only
- Farmer mapping using Centre UID + Mitra UID with hierarchy validation
- UFC Admin and UFC Mitra profile-completion flows with document upload
- Validation queues with Pending / Approved / Rejected flow
- AVPL visibility of uploaded documents and images
- Separate dashboards for all requested roles
- Master-data views
- Products / Orders / Transactions / LMS / Support / Marketplace placeholders with schema-ready routes
- Audit log support
- Seed script and sample data
- Requirement coverage document and route map

## Roles
- super_admin
- avpl_admin
- accounts
- sales_nelocals
- sales_unnatfarm
- ufc_admin
- ufc_mitra
- farmer

## Collections
- users
- farmer_master
- ufc_admin_master
- ufc_mitra_master
- documents
- validations
- products
- orders
- transactions
- lms_materials
- audit_logs
- support_tickets
- insurance_requests
- finance_requests
- trader_onboarding
- marketplace_posts

## Quick start
```bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
# or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
python scripts/seed_demo.py
python run.py
```



