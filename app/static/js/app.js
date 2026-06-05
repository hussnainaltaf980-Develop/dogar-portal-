// Dogar Trading Portal - Shared JS

// Setup axios to send credentials
axios.defaults.withCredentials = true;

// Logout
async function logout() {
    if (!confirm('Are you sure you want to logout?')) return;
    try { await axios.post('/api/auth/logout'); } catch (e) {}
    localStorage.removeItem('user');
    window.location.href = '/login';
}

// Global axios error handler: redirect to login on 401
axios.interceptors.response.use(
    r => r,
    err => {
        if (err.response && err.response.status === 401 && !window.location.pathname.includes('/login')) {
            window.location.href = '/login';
        }
        return Promise.reject(err);
    }
);

// Helpers
function statusBadge(status) {
    if (!status) return '<span class="badge badge-pending">—</span>';
    return `<span class="badge badge-${status.toLowerCase().replace(/\s/g,'-')}">${status}</span>`;
}
function fmtDate(d) {
    if (!d) return '—';
    return dayjs(d).format('DD-MMM-YYYY');
}
function fmtDateTime(d) {
    if (!d) return '—';
    return dayjs(d).format('DD-MMM-YYYY HH:mm');
}
function fmtMoney(v) {
    return (Number(v) || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m]);
}

// Simple modal helper
function openModal(html) {
    const wrap = document.createElement('div');
    wrap.className = 'modal-overlay';
    wrap.innerHTML = `<div class="modal-box">${html}</div>`;
    wrap.addEventListener('click', e => {
        if (e.target === wrap) closeModal();
    });
    document.body.appendChild(wrap);
    return wrap;
}
function closeModal() {
    document.querySelectorAll('.modal-overlay').forEach(el => el.remove());
}

function toast(msg, type='success') {
    const colors = { success: 'bg-emerald-500', error: 'bg-red-500', info: 'bg-blue-500' };
    const t = document.createElement('div');
    t.className = `fixed top-5 right-5 z-[100] ${colors[type]} text-white px-4 py-2 rounded-lg shadow-lg text-sm`;
    t.innerHTML = `<i class="fa-solid fa-${type==='success'?'check':'info'}-circle mr-2"></i>${msg}`;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3000);
}
