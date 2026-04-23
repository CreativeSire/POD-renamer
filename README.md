# POD Renamer — DALA Technologies

AI-powered Proof of Delivery renaming system with full history and batch logging.

## Features
- DALA invoice processing → `Store - Location DT-InvoiceNo - DD-MM-YYYY.pdf`
- Brand Partner invoice processing → `Store - Location AG-InvoiceNo DDMMYY.pdf`
- Auto-detects brand (FlozzyD, Prothrive, Whole Eat, Medi Tea, Etifarm, August Secret)
- Real image-to-PDF conversion (jsPDF)
- Direct folder output via File System Access API (no Windows security warnings)
- Full batch history and logs with Pass / Review status
- Dashboard with stats and top stores

---

## Deploy to Railway

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/CreativeSire/POD-renamer.git
git push -u origin main
```

### 2. Create Railway project
- Go to railway.app → New Project → Deploy from GitHub repo
- Select `POD-renamer`

### 3. Add PostgreSQL database
- In Railway project → New → Database → PostgreSQL
- Railway will auto-set `DATABASE_URL` in your environment

### 4. Set environment variables
In Railway → your service → Variables, add:

| Variable | Value |
|----------|-------|
| `GEMINI_API_KEY` | Your Gemini API key from aistudio.google.com |
| `GEMINI_MODEL` | Gemini model used for extraction, defaults to `gemini-2.5-flash` |
| `GEMINI_MODEL_FALLBACKS` | Comma-separated fallback models for temporary Gemini failures |
| `SECRET_KEY` | Any long random string (e.g. `openssl rand -hex 32`) |
| `ADMIN_USERNAME` | First admin login username, defaults to `admin` |
| `ADMIN_PASSWORD` | First admin password. Set this before first deploy |
| `ADMIN_FULL_NAME` | Display name for the first admin user |

`DATABASE_URL` is set automatically by Railway when you add PostgreSQL.

### 5. Deploy
Railway auto-deploys on every push to main. The database tables are created automatically on first boot.

---

## First Login
- **Username:** value of `ADMIN_USERNAME`, or `admin` if not set
- **Password:** value of `ADMIN_PASSWORD`, or `admin123` if not set

> Set `ADMIN_PASSWORD` in Railway before the first deploy. If the admin user already exists, changing this variable will not update that existing user's password.

---

## Local Development
```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your DATABASE_URL, SECRET_KEY, GEMINI_API_KEY, and ADMIN_PASSWORD
python app.py
```

---

## Brand Partners
| Brand | Code |
|-------|------|
| FlozzyD | AG |
| August Secret | AG |
| Prothrive | PH |
| Whole Eat | WH |
| Medi Tea | WH |
| Etifarm | ET |
