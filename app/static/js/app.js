async function fetchJson(url) {
  const r = await fetch(url);
  if (!r.ok) return null;
  return await r.json();
}

function setFieldValue(name, value) {
  const el = document.querySelector(`[name="${name}"]`);
  if (!el || value === undefined || value === null || value === '') return;

  if (el.tagName === 'SELECT') {
    const exists = [...el.options].some(o => o.value === value);
    if (!exists) {
      el.add(new Option(value, value));
    }
  }

  el.value = value;
}

async function applyCentre(uid) {
  if (!uid) return;

  const d = await fetchJson(`/api/centre/${encodeURIComponent(uid)}`);
  if (!d || !d.ok) return;

  ['state', 'district', 'block', 'village'].forEach(k => {
    setFieldValue(k, d[k]);
  });
}

async function applyMitra(uid) {
  if (!uid) return;

  const d = await fetchJson(`/api/mitra/${encodeURIComponent(uid)}`);
  if (!d || !d.ok) return;

  setFieldValue('centre_uid', d.centre_uid || '');

  ['state', 'district', 'block', 'village'].forEach(k => {
    setFieldValue(k, d[k]);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const centre = document.querySelector('[data-centre-lookup]');
  if (centre) {
    centre.addEventListener('change', () => applyCentre(centre.value));
    if (centre.value) applyCentre(centre.value);
  }

  const mitra = document.querySelector('[data-mitra-lookup]');
  if (mitra) {
    mitra.addEventListener('change', () => applyMitra(mitra.value));
    if (mitra.value) applyMitra(mitra.value);
  }

  const role = document.querySelector('[name="role"]');
  if (role) {
    const refresh = () => {
      document.querySelectorAll('[data-role-field]').forEach(x => {
        const roles = x.dataset.roleField.split(',');
        x.style.display = roles.includes(role.value) ? 'block' : 'none';
      });
    };

    role.addEventListener('change', refresh);
    refresh();
  }

  document.querySelectorAll('.uid-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.closest('.uid-card')?.querySelector('.masked-uid');
      if (!target) return;

      const real = target.dataset.realUid || '';
      const showing = target.dataset.showing === '1';

      target.textContent = showing ? '********' : real;
      target.dataset.showing = showing ? '0' : '1';
    });
  });

  const farmerForm = document.getElementById('farmerRegistrationForm');
  if (farmerForm) {
    farmerForm.addEventListener('submit', e => {
      const centre = farmerForm.querySelector('[name="centre_uid"]')?.value.trim();
      const mitra = farmerForm.querySelector('[name="mitra_uid"]')?.value.trim();

      if (!centre || !mitra) {
        e.preventDefault();
        alert('Centre UID and UFC Mitra UID are mandatory for farmer registration.');
      }
    });
  }
});