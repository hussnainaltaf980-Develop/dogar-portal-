# 🚀 Deployment Guide - Dogar Trading Portal

## Quick Start Deployment (5 minutes)

This guide walks you through deploying your production-ready FastAPI application to **Render**.

---

## 📋 Prerequisites

- ✅ GitHub account (you have this)
- ✅ Render account (free at https://render.com)
- ✅ Your repository is public or private (doesn't matter)

---

## 🔑 Step 1: Generate SECRET_KEY

Run this command locally to generate a secure key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

**Example output:**
```
a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8c9d0e1f2
```

**Save this value** — you'll need it in Step 3.

---

## 🌐 Step 2: Connect to Render

### 2.1 Create Render Account
1. Go to **https://render.com**
2. Click **"Sign up"**
3. Choose **"Continue with GitHub"**
4. Authorize Render to access your GitHub repos

### 2.2 Create Web Service
1. Click **"New +"** (top right)
2. Select **"Web Service"**
3. Look for `dogar-portal-` repository
4. Click **"Connect"**

---

## ⚙️ Step 3: Configure Deployment Settings

### 3.1 Basic Settings

| Field | Value |
|-------|-------|
| **Name** | `dogar-portal` (or any name) |
| **Environment** | `Python 3` |
| **Region** | Select closest to your users (e.g., `Singapore`, `Frankfurt`, `US East`) |
| **Branch** | `main` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |

### 3.2 Environment Variables

Click **"Advanced"** → **"Add Environment Variable"**

Add these variables one by one:

| Key | Value | Example |
|-----|-------|---------|
| `ENV` | `production` | `production` |
| `SECRET_KEY` | *Your generated key from Step 1* | `a1b2c3d4e5f6...` |
| `DEFAULT_ADMIN_EMAIL` | Your email | `hussnainmr07@gmail.com` |
| `DEFAULT_ADMIN_PASSWORD` | Strong password (≥12 chars) | `YourSecurePass123!@#` |
| `DEFAULT_ADMIN_NAME` | Admin name | `Administrator` |
| `COMPANY_NAME` | Company name | `Dogar Trading Corporation` |
| `COMPANY_TAGLINE` | Company tagline | `Global Trading & Overseas Employment Solutions` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | `1440` |
| `DATABASE_URL` | `sqlite:///./data/dogar_trading.db` | `sqlite:///./data/dogar_trading.db` |
| `SEED_DEMO_DATA` | `false` | `false` |
| `AUTO_RESTORE_BUNDLED_SQL` | `false` | `false` |

**Password Requirements:**
- ✅ At least 12 characters
- ✅ Mix of letters, numbers, symbols
- ✅ Examples: `Portal@2026Secure!`, `Dogar#Trading123`

---

## 🚀 Step 4: Deploy

1. Scroll to bottom
2. Click **"Create Web Service"**
3. **Wait 2-3 minutes** while Render:
   - Clones your repository
   - Installs dependencies (`pip install -r requirements.txt`)
   - Starts the FastAPI server
   - Assigns a public URL

### What to see during deployment:

```
Building...
[1/3] Installing dependencies
[2/3] Building application
[3/3] Starting service
✓ Live on https://dogar-portal.onrender.com
```

---

## ✅ Step 5: Test Your Deployment

Once deployed:

1. **Open your app:** https://dogar-portal.onrender.com
2. **Login with:**
   - Email: `hussnainmr07@gmail.com` (or your chosen email)
   - Password: Your chosen password
3. **First login:** You'll be forced to change the password
4. **Dashboard:** Explore your production app!

---

## 📊 Monitor Your Deployment

After deployment is live:

1. Go to your **Render dashboard**
2. Click on **`dogar-portal`** service
3. View:
   - ✅ **Logs** — real-time server output
   - ✅ **Metrics** — CPU, memory, requests
   - ✅ **Events** — deployment history

---

## 🔄 Auto-Deploy on Every Push

Your app will **automatically redeploy** every time you push to GitHub:

```bash
git add .
git commit -m "Your changes"
git push origin main
```

Render watches your repo and re-deploys instantly! 🎉

---

## 💾 Optional: Add Persistent Storage

If you want the database to survive server restarts:

1. In Render dashboard, click on `dogar-portal`
2. Go to **"Disks"** section
3. Click **"Create Disk"**
4. Set:
   - **Path:** `/data`
   - **Size:** `1 GB` (free tier)
5. Save

Your SQLite database will now persist across rebuilds.

---

## 🆘 Troubleshooting

### "Deploy Failed"
- Check the **Logs** tab in Render
- Ensure all environment variables are set correctly
- Verify `requirements.txt` is in your repo root

### "Application Error"
- Check Render logs for error messages
- Verify `DEFAULT_ADMIN_PASSWORD` meets strength requirements (≥12 chars)
- Ensure `SECRET_KEY` is 64 characters

### "Port Error"
- Make sure your `Procfile` uses `$PORT` variable
- ✅ Correct: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- ❌ Wrong: `uvicorn app.main:app --host 0.0.0.0 --port 8000`

### "Database Not Found"
- Render will create `/data/` directory automatically
- First run will initialize the SQLite database
- Check app logs to confirm database creation

---

## 📚 Additional Resources

| Topic | Link |
|-------|------|
| Render Docs | https://render.com/docs |
| FastAPI Docs | https://fastapi.tiangolo.com |
| Your App Swagger API | `https://dogar-portal.onrender.com/api/docs` |
| Your App ReDoc | `https://dogar-portal.onrender.com/api/redoc` |

---

## 🎯 Next Steps After Deployment

1. ✅ **Test all features** in production
2. ✅ **Set up custom domain** (optional, in Render settings)
3. ✅ **Enable HTTPS** (automatic on Render)
4. ✅ **Monitor logs** regularly
5. ✅ **Create backup** of your database periodically

---

## 📞 Need Help?

If deployment fails:

1. Check **Render Logs** (tab in service dashboard)
2. Review **Environment Variables** (all set correctly?)
3. Verify **`requirements.txt`** exists in repo root
4. Ensure **`Procfile`** is in repo root

---

**Your app is production-ready! Deploy now and share your URL with your team.** 🚀

Last Updated: June 2026  
Repository: https://github.com/hussnainaltaf980-Develop/dogar-portal-
