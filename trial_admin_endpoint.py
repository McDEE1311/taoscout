# ── ADD TO api.py ──────────────────────────────────────────────────────────────
# Admin endpoint to manually grant trial access to existing subscribers

@app.post("/admin/send-trial")
async def admin_send_trial(email: str, days: int = 3, key: str = Depends(verify_key)):
    """Manually send free trial access to an email address (existing subscriber or new)."""
    from taoscout_auth import create_trial_user
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Invalid email address")
    result = create_trial_user(email.strip().lower(), trial_days=days, source="admin_manual")
    return result


# ── ALSO UPDATE: /subscribe endpoint to trigger trial creation ─────────────────
# Replace the existing @app.post("/subscribe") handler with this version:
#
# @app.post("/subscribe")
# async def subscribe(body: SubscribeRequest):
#     email = body.email.strip().lower()
#     if not is_valid_email(email):
#         raise HTTPException(status_code=400, detail="Invalid email address")
#     try:
#         conn = sqlite3.connect(str(SUBSCRIBERS_DB))
#         conn.execute(
#             "INSERT INTO subscribers (email, source) VALUES (?, ?)",
#             (email, body.source)
#         )
#         conn.commit()
#         conn.close()
#     except sqlite3.IntegrityError:
#         pass  # already subscribed — still try to create/resend trial below
#     except Exception:
#         raise HTTPException(status_code=500, detail="Could not save subscription")
#
#     # NEW: create trial user + send setup email
#     try:
#         from taoscout_auth import create_trial_user
#         create_trial_user(email, trial_days=3, source="landing_trial")
#     except Exception as e:
#         print(f"[TRIAL CREATE ERROR] {email}: {e}")
#         # Don't fail the subscribe call if trial creation has an issue —
#         # they're still saved in subscribers.db and can be retried via admin endpoint
#
#     return {"status": "ok", "message": "You're on the list! Check your email for trial access."}
