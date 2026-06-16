# Run this on the rig to patch landing.html with beta messaging
from pathlib import Path

p = Path("landing.html")
s = p.read_text()

# 1. Update hero signup success message in JS
old_success = "msg.textContent = '✓ You\\'re on the list! We\\'ll be in touch.';"
new_success = "msg.textContent = \"✓ You're on the beta list. Free Operator access is being approved manually. We'll email your access instructions within 24 hours.\";"

# 2. Update hero note
old_note = '<p class="hero-note">Free during beta. No spam. Unsubscribe anytime.</p>'
new_note = '<p class="hero-note">48-hour free Operator beta. Manual approval. Access instructions sent by email.</p>'

# 3. Update footer email section description
old_email_desc = '<p>Be first to access daily subnet intelligence reports, emission alerts, and operator tools. Free during beta.</p>'
new_email_desc = '<p>Join the 48-hour free Operator beta. We approve access manually and email you instructions within 24 hours.</p>'

# 4. Add beta process section after pricing grid
old_after_pricing = '</section>\n\n<!-- EMAIL CAPTURE -->'
new_after_pricing = '''  <div style="margin-top:3rem;background:var(--bg2);border:1px solid var(--border);border-radius:12px;padding:2rem;max-width:600px">
    <div style="font-size:11px;color:var(--cyan);text-transform:uppercase;letter-spacing:2px;margin-bottom:1rem">Beta Access Process</div>
    <div style="display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;gap:16px;align-items:flex-start">
        <span style="color:var(--cyan);font-size:18px;font-weight:700;flex-shrink:0">1</span>
        <div>
          <div style="font-size:14px;color:var(--text);margin-bottom:2px">Join with email</div>
          <div style="font-size:12px;color:var(--muted);font-family:sans-serif">Enter your email below or in the hero section above.</div>
        </div>
      </div>
      <div style="display:flex;gap:16px;align-items:flex-start">
        <span style="color:var(--cyan);font-size:18px;font-weight:700;flex-shrink:0">2</span>
        <div>
          <div style="font-size:14px;color:var(--text);margin-bottom:2px">Manual approval</div>
          <div style="font-size:12px;color:var(--muted);font-family:sans-serif">We review and approve beta users within 24 hours.</div>
        </div>
      </div>
      <div style="display:flex;gap:16px;align-items:flex-start">
        <span style="color:var(--cyan);font-size:18px;font-weight:700;flex-shrink:0">3</span>
        <div>
          <div style="font-size:14px;color:var(--text);margin-bottom:2px">Receive access instructions</div>
          <div style="font-size:12px;color:var(--muted);font-family:sans-serif">We email you an API key and dashboard link.</div>
        </div>
      </div>
      <div style="display:flex;gap:16px;align-items:flex-start">
        <span style="color:var(--cyan);font-size:18px;font-weight:700;flex-shrink:0">4</span>
        <div>
          <div style="font-size:14px;color:var(--text);margin-bottom:2px">Paid plans open after beta</div>
          <div style="font-size:12px;color:var(--muted);font-family:sans-serif">$10/mo Miner and $29/mo Operator plans launch after the 48-hour beta window.</div>
        </div>
      </div>
    </div>
  </div>

</section>

<!-- EMAIL CAPTURE -->'''

# Apply patches
changes = 0
if old_success in s:
    s = s.replace(old_success, new_success)
    changes += 1
    print("✓ Patched success message")
else:
    print("✗ Success message not found")

if old_note in s:
    s = s.replace(old_note, new_note)
    changes += 1
    print("✓ Patched hero note")
else:
    print("✗ Hero note not found")

if old_email_desc in s:
    s = s.replace(old_email_desc, new_email_desc)
    changes += 1
    print("✓ Patched email section description")
else:
    print("✗ Email description not found")

if old_after_pricing in s:
    s = s.replace(old_after_pricing, new_after_pricing)
    changes += 1
    print("✓ Added beta process section")
else:
    print("✗ Pricing section end not found")

p.write_text(s)
print(f"\n{changes}/4 patches applied")
