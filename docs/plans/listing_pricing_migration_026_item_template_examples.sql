-- Migration 026: example item_template rows — a working copy of the
-- current Python-built defaults (importer/ebay_descriptions.py's
-- _nav_cell_html/_era_list_cell_html/_chip_html, with the live
-- description_theme_settings values baked in as of 8/09), so Fei has a
-- real starting point to edit instead of a blank textarea.
--
-- Deliberately seeded under "_example" keys, NOT the reserved live keys
-- (family_tile/family_tile_current/era_row/era_chip/era_chip_current) —
-- a static item_template always renders <img>, where the Python default
-- falls back to a gray placeholder box when nav_image_url is still NULL.
-- Seeding under the reserved keys would have silently broken images on
-- any listing without a photo yet. Copy the HTML into a real key (the
-- reserved ones, or your own + {{family_nav:your_key}}) when ready.

BEGIN;

INSERT INTO description_sections (key, label, html, kind, sort_order) VALUES

('family_tile_example', 'Example: family_tile (not current)',
$ex$<a href="{{item_url}}" style="display:block;text-decoration:none;"><table role="presentation" width="100%" bgcolor="#12365c" cellpadding="0" cellspacing="0" style="background:#12365c;border:1px solid #1d4d7a;border-radius:11px;"><tr><td style="padding:14px;text-align:center;"><span style="display:block;font-size:10px;color:#7fa8c9;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;font-family:sans-serif;">{{item_label}}</span><img src="{{item_image_url}}" alt="{{item_label}}" style="width:140px;height:195px;object-fit:cover;border-radius:5px;display:block;margin:0 auto 10px;"><span style="display:inline-block;font-size:12px;font-weight:bold;color:#0a1f38;background:#3fc3e8;border-radius:7px;padding:7px 16px;font-family:sans-serif;">View listing</span></td></tr></table></a>$ex$,
'item_template', 1),

('family_tile_current_example', 'Example: family_tile_current',
$ex$<table role="presentation" width="100%" bgcolor="#12365c" cellpadding="0" cellspacing="0" style="background:linear-gradient(155deg,#12365c,#0a1f38);border:2px solid #3fc3e8;border-radius:11px;box-shadow:0 0 14px rgba(63,195,232,0.3);"><tr><td style="padding:14px;text-align:center;"><span style="display:block;font-size:10px;color:#3fc3e8;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;font-family:sans-serif;">{{item_label}}</span><img src="{{item_image_url}}" alt="{{item_label}}" style="width:140px;height:195px;object-fit:cover;border-radius:5px;display:block;margin:0 auto 10px;"><span style="display:inline-block;font-size:11px;font-weight:bold;color:#0a1f38;background:#fff;border-radius:7px;padding:7px 14px;font-family:sans-serif;">Viewing this</span></td></tr></table>$ex$,
'item_template', 2),

('era_row_example', 'Example: era_row',
$ex$<a href="{{item_url}}" style="display:block;text-decoration:none;"><table role="presentation" width="100%" bgcolor="#12365c" cellpadding="0" cellspacing="0" style="background:#12365c;border:1px solid #1d4d7a;border-radius:9px;"><tr><td style="padding:11px 12px;"><table role="presentation" cellpadding="0" cellspacing="0"><tr><td valign="middle" style="padding-right:10px;"><span style="display:inline-block;width:32px;height:45px;border-radius:4px;background:#1d4d7a;"></span></td><td valign="middle"><span style="display:block;font-size:13px;font-weight:bold;color:#eaf6fb;font-family:sans-serif;">{{item_label}}</span><span style="display:block;font-size:11px;color:#3fc3e8;font-family:sans-serif;">Shop this set &rsaquo;</span></td></tr></table></td></tr></table></a>$ex$,
'item_template', 3),

('era_chip_example', 'Example: era_chip (not current)',
$ex$<a href="{{item_url}}" style="display:inline-block;margin:2px;padding:4px 10px;border-radius:12px;font-family:sans-serif;font-size:12px;text-decoration:none;background:transparent;border:1px solid #1d4d7a;color:#9db2c6;">{{item_label}}</a>$ex$,
'item_template', 4),

('era_chip_current_example', 'Example: era_chip_current',
$ex$<span style="display:inline-block;margin:2px;padding:4px 10px;border-radius:12px;font-family:sans-serif;font-size:12px;text-decoration:none;background:#3fc3e8;color:#0a1f38;">{{item_label}} (you're here)</span>$ex$,
'item_template', 5)

ON CONFLICT (key) DO NOTHING;

COMMIT;
