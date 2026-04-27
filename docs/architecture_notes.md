# Architecture Notes

## Design goal
The application is intentionally structured so the current web MIS can later expose API endpoints for separate UFC Admin, UFC Mitra, and Farmer mobile apps without needing to redesign core persistence or business relationships.

## Key principles
1. Authentication data is isolated from operational master data.
2. Unique IDs are persistent keys for hierarchy mapping.
3. Approval state controls access to operational modules.
4. Every important state change is auditable.
5. Role dashboards operate from dedicated service methods rather than embedded route logic.

## Collections
### users
Authentication and account lifecycle metadata.

### ufc_admin_master
Operational centre profile record, linked to the auth user.

### ufc_mitra_master
Operational mitra profile record, linked to auth user and mapped to a centre.

### farmer_master
Operational farmer record, linked to auth user and mapped to centre + mitra.

### documents
Stores document metadata and file path references.

### validations
Central validation queue abstraction across UFC Admin / UFC Mitra / Farmer.

### products / orders / transactions
Business workflow support for current MVP and future expansion.

## Route organization
- `auth`: login, logout, registration, profile completion
- `dashboard`: role home pages
- `admin`: account creation, product add, LMS upload
- `validations`: queue, detail, approve/reject
- `master_data`: data visibility pages
- `documents`: secure file serving
- `modules`: business module pages

## Future API split
This project can be evolved into:
- `/api/auth`
- `/api/farmers`
- `/api/mitras`
- `/api/centres`
- `/api/orders`
- `/api/transactions`
while keeping the same services and collection structure.
