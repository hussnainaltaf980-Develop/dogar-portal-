# Deployment Guide — Dogar Trading Corporation Portal

**Built by HussnainTechVertex Pvt Ltd.**

This project is a self-contained FastAPI application with SQLite storage,
Jinja2 templates, and a static-asset folder. It is ready for hosting on:

- A standalone VM / VPS (Ubuntu 22.04+, Debian, RHEL, …)
- Docker (Dockerfile-ready stack)
- PaaS providers that support long-running Python processes
  (Railway, Render, Fly.io, DigitalOcean App Platform, …)

> ⚠️ **NOT supported**: Cloudflare Workers, Cloudflare Pages, Vercel
> Edge Functions, AWS Lambda. The portal needs a long-running Python
> process and writable disk for the SQLite database.

---

## 1) Quick start (development sandbox)

```bash
# 1. Extract the zip
unzip dogar-portal_*_HTV-Pvt-Ltd.zip
cd dogar-portal

# 2. Create a Python virtual env (Python 3.11+ recommended)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. (Optional) Create .env file from template
cp .env.example .env
# edit .env and set SECRET_KEY, DATABASE_URL, etc.

# 5. Start the server (foreground)
uvicorn app.main:app --host 0.0.0.0 --port 3000

# 6. Open http://localhost:3000
#    Login with the credentials you set in .env (DEFAULT_ADMIN_EMAIL /
#    DEFAULT_ADMIN_PASSWORD) — you will be forced to change the
#    password on first login.
```

The bundled `migrations/dogar_full_backup.sql.gz` contains the full
real production dataset (2,674 candidates, 2,260 demands, 1,243 clients,
18 document templates with all field positions). On first start the
app auto-detects an empty DB and restores from this dump — no extra
operator action required.

---

## 2) Production with PM2 (recommended)

```bash
# Install PM2 (Node.js required, only for the daemon manager)
npm install -g pm2

# Start the portal as a daemon
cd dogar-portal
pm2 start ecosystem.config.cjs
pm2 save

# Auto-start on system reboot
pm2 startup
# (copy & run the command pm2 prints)

# Logs (non-blocking)
pm2 logs dogar-portal --nostream

# Restart after code changes
pm2 restart dogar-portal --update-env
```

---

## 3) Behind nginx (recommended for public hosting)

```nginx
server {
    listen 80;
    server_name portal.example.com;

    client_max_body_size 16M;

    location /static/ {
        alias /opt/dogar-portal/app/static/;
        expires 30d;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
    }
}
```

Add HTTPS via certbot:
```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d portal.example.com
```

---

## 4) Docker

Create a `Dockerfile` next to this guide:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# OS deps for Pillow / qrcode / reportlab
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg-dev zlib1g-dev libfreetype6-dev \
        libxml2 libxslt1.1 libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
```

Build & run:
```bash
docker build -t dogar-portal:htv .
docker run -d --name dogar-portal \
    -p 3000:3000 \
    -v $(pwd)/data:/app/data \
    -e SECRET_KEY="change-me-please" \
    dogar-portal:htv
```

---

## 5) Environment variables (`.env`)

```ini
# Required — change in production!
SECRET_KEY=your-256-bit-random-secret

# Optional — defaults to SQLite at data/dogar_trading.db
DATABASE_URL=sqlite:///./data/dogar_trading.db

# Optional — for the LLM-powered DtcBot brain (falls back to rule-based
# agent when missing or invalid)
OPENAI_API_KEY=
OPENAI_BASE_URL=

# Optional — JWT token lifetime in minutes (default 60*24*30 = 30 days)
ACCESS_TOKEN_EXPIRE_MINUTES=43200
```

---

## 6) Bootstrap login

Use the credentials you configured in `.env`:

```
Email:    <value of DEFAULT_ADMIN_EMAIL>
Password: <value of DEFAULT_ADMIN_PASSWORD>
```

The bootstrap admin is created with the `must_change_password` flag set,
so the UI will redirect you to a forced-password-change screen on first
login. The endpoint `POST /api/auth/change-password` enforces:

  - new password ≥ 12 characters
  - new password not in the well-known weak-list
  - new password ≠ current password

In `ENV=production` mode the server refuses to start if the `.env` still
contains any of the documented weak defaults — there is no way to ship
to production with `admin123` as the live admin password.

---

## 7) Health-check endpoint

```bash
curl http://localhost:3000/api/ping
# → {"ok": true, "service": "dogar-portal", "version": "..."}
```

---

## 8) Branding

This project is developed by **HussnainTechVertex Pvt Ltd.** The brand
logo (`app/static/img/dev_logo.png`) appears in:

- Application footer (every page)
- Login page footer
- Every printed E-Barcode sheet
- Every printed Demand File Receipt and Payment Receipt

To re-brand for another deployment, replace the three `dev_logo*.png`
files and grep-replace `HussnainTechVertex Pvt Ltd.` in
`app/templates/base.html`, `app/templates/login.html`, and
`app/services/letterhead_renderer.py`.

---

© 2025–2026 · Built and maintained by **HussnainTechVertex Pvt Ltd.**
