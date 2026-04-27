# UnnatFarm MIS - Final Integrated Update

Included fixes and upgrades:

- India location hierarchy support through `location_master` with all States/UTs and starter district/block/village data.
- Dynamic dropdown APIs:
  - `/api/locations/states`
  - `/api/locations/districts`
  - `/api/locations/blocks`
  - `/api/locations/villages`
- UFC Admin UID format based on state code, e.g. `AS0001`.
- UFC Mitra UID format under centre UID, e.g. `AS0001-01`.
- UFC Mitra creation is available to UFC Admin and is mapped to that centre.
- Farmer registration auto-maps by Mitra UID and fills centre/location data while keeping dropdowns editable.
- UFC Admin and UFC Mitra profile completion includes state/district/block/village.
- AVPL validation screen now shows linked uploaded documents with Preview/View and Download links.
- Fixed empty table Jinja errors.
- Fixed seed/index duplicate issues.
- Seed creates only Super Admin and AVPL Admin.

Default seed users:

- `superadmin / admin123`
- `avpladmin / admin123`

Run:

```powershell
python scripts/seed_demo.py
python run.py
```

## Location System Simplification Update

- State is the only fixed dropdown field.
- District, block, and village are manual text fields because complete master data is not available yet.
- When a UFC Admin enters district/block/village, those values are stored in the UFC Admin master record.
- When a UFC Mitra enters/applies the Centre UID, the system auto-fills state, district, block, and village from the mapped centre, but keeps all fields editable.
- When a Farmer enters/applies the Mitra UID, the system auto-fills Centre UID plus state, district, block, and village from the mapped Mitra/Centre, but keeps fields editable.
- The backend still exposes learned district/block/village APIs using values already entered in user/master records, so the system can grow naturally without hardcoded district/village master data.

## 2026-04-24 Rejection Correction Flow Update
- Added rejected-application popup for UFC Admin and UFC Mitra.
- Continue button opens the same registration/profile form.
- Previous submitted values are loaded back into the form for correction.
- AVPL rejection remarks are shown above the form as rectification instructions.
- Resubmission changes status back to pending and creates a fresh pending validation if the previous one was rejected.
- File uploads are optional during correction; upload again only when documents need correction.
- Login page sidebar is hidden even when an old session exists.

## 2026-04-26 Rejection Continue Button Final Fix
- Removed the old rejected-application popup from `app/templates/dashboard/pending_access.html`.
- Removed the JavaScript submit interception that caused the Continue button to refresh instead of redirecting.
- Replaced Continue with a direct normal anchor link to the correction form.
- UFC Admin rejected users continue to `/profile/ufc-admin/complete`.
- UFC Mitra rejected users continue to `/profile/ufc-mitra/complete`.
- Existing submitted master data remains pre-filled in the correction form.
- Rejection reason is shown on the pending access page and again above the editable correction form.
- Cleaned the duplicated `/dashboard/pending-access` route decorator in `app/blueprints/dashboard/routes.py`.
