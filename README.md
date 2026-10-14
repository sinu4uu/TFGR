# Payment Verify API - Render Deployment Guide

## 1. Web Service Settings on Render
When creating a **New Web Service** on [Render](https://dashboard.render.com/):

- **Environment**: `Python`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT api.index:app`
- **Health Check Path**: `/api/health`

> **Note on Workers**: `--workers 1 --threads 4` is essential because the API uses a live in-memory cache and a real-time IMAP push listener (`background_idle_listener`). Running with 1 worker prevents duplicate IMAP connections to Gmail.

---

## 2. Environment Variables (Render Dashboard)
Add these in the **Environment** tab:

| Variable | Value / Description | Required |
|---|---|---|
| `PYTHON_VERSION` | `3.11.9` | Recommended |
| `EMAIL_ADDRESS` | Your Gmail address receiving alerts | Yes |
| `EMAIL_APP_PASS` | 16-character Google App Password | Yes |
| `MAX_AGE_SECONDS`| `86400` (24 Hours) | Optional |

---

## 3. Endpoints

- `GET /api/health` - Health check status
- `GET /api/verify?order_id=<ORDER_ID>&amount=<AMOUNT>` - Verify order payment
- `POST /api/verify` - Verify JSON `{ "order_id": "...", "amount": 100 }`
- `GET /api/fampay?id=<UTR>` - Direct UTR / Transaction ID verification
