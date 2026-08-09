-- Migration 022: Deep Sea theme content for description_sections.
-- Adapted from the approved Deep Sea mockup (BiggyFish Card Shop),
-- docs/plans/listing-pricing-system.md "Reusable section library" +
-- "Full match" restyle of the token-block renderers (importer/
-- ebay_descriptions.py DEEP_SEA palette, 8/08). Sections are flat
-- (no corner-rounding) so they compose seamlessly whether inserted
-- standalone or concatenated into a layout; each layout row wraps its
-- concatenated sections in one outer rounded/bordered container.
--
-- rarity_chips is STATIC content (a few example tags) — Fei hand-edits
-- which rarities apply per listing. Not a dynamic token; that's a
-- separate, still-undecided feature.

BEGIN;

INSERT INTO description_sections (key, label, html, kind, sort_order) VALUES

('header', 'Header (logo + trust line)',
$sec$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0a1f38,#123a63);">
<tr><td style="padding:24px;text-align:center;border-bottom:1px solid #1d4d7a;">
<span style="display:inline-block;width:56px;height:56px;border-radius:50%;background:#3fc3e8;box-shadow:0 0 18px rgba(63,195,232,0.6);"></span>
<p style="margin:12px 0 0;font-size:22px;font-weight:900;color:#fff;letter-spacing:0.06em;font-family:sans-serif;">BIGGYFISH <span style="color:#3fc3e8;">CARD SHOP</span></p>
<p style="margin:6px 0 0;font-size:11px;color:#7fa8c9;letter-spacing:0.05em;font-family:sans-serif;">100% AUTHENTIC &middot; 88K+ SOLD &middot; 100% POSITIVE</p>
</td></tr></table>
$sec$, 'section', 1),

('set_band', 'Set title band',
$sec$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:26px 24px 6px;text-align:center;">
<span style="display:inline-block;border:1px solid #3fc3e8;color:#3fc3e8;font-size:11px;font-weight:bold;letter-spacing:0.14em;padding:4px 14px;border-radius:14px;font-family:sans-serif;">{{series_name}}</span>
<p style="margin:12px 0 0;font-size:30px;font-weight:900;color:#fff;line-height:1.05;font-family:sans-serif;">{{set_name}}</p>
<p style="margin:4px 0 0;font-size:15px;color:#9db2c6;font-family:sans-serif;">Singles</p>
</td></tr></table>
$sec$, 'section', 2),

('intro_trust', 'Intro + trust callout',
$sec$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:22px 24px 6px;">
<p style="margin:0 0 12px;font-size:14.5px;line-height:1.6;color:#c3d4e3;text-align:center;font-family:sans-serif;">Take your pick from the drop-down above &mdash; all cards <b style="color:#fff;">NM&ndash;M (Pack Fresh)</b>.</p>
<table role="presentation" width="100%" bgcolor="#12365c" cellpadding="0" cellspacing="0" style="background:rgba(63,195,232,0.1);border:1px solid #1d4d7a;border-radius:9px;">
<tr><td style="padding:12px 14px;text-align:center;"><p style="margin:0;font-size:13px;color:#9fe0f2;font-family:sans-serif;">&#10003; Cards shown with front &amp; back images are the exact card you'll receive.</p></td></tr>
</table>
</td></tr></table>
$sec$, 'section', 3),

('rarity_chips', 'Rarity chips (edit tags per listing)',
$sec$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:22px 24px 0;">
<p style="margin:0 0 10px;font-size:11px;font-weight:bold;color:#7fa8c9;text-transform:uppercase;letter-spacing:0.08em;font-family:sans-serif;">Rarities you'll find</p>
<p style="margin:0;line-height:2;">
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Full Art</span>
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Alternate Art</span>
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Illustration Rare</span>
</p>
</td></tr></table>
$sec$, 'section', 4),

('support', 'Support / shipping',
$sec$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:24px 24px 22px;border-top:1px solid #1d4d7a;">
<p style="margin:0 0 6px;font-size:14px;font-weight:bold;color:#fff;font-family:sans-serif;">Questions? International or shipping?</p>
<p style="margin:0 0 10px;font-size:13px;color:#9db2c6;line-height:1.6;font-family:sans-serif;">Reach out anytime &mdash; I'll take good care of you.</p>
<p style="margin:0;font-size:13px;color:#9db2c6;line-height:1.6;font-family:sans-serif;"><b style="color:#c3d4e3;">Packaging:</b> penny sleeve + top loader, PWE with tracking. Combined shipping on multiples.</p>
</td></tr></table>
$sec$, 'section', 5),

('footer', 'Footer / store button',
$sec$<table role="presentation" width="100%" bgcolor="#e8467e" cellpadding="0" cellspacing="0" style="background:#e8467e;">
<tr><td style="padding:14px;text-align:center;"><a href="#" style="font-size:13px;font-weight:900;color:#fff;text-decoration:none;letter-spacing:0.04em;font-family:sans-serif;">VISIT OUR STORE &rsaquo;</a></td></tr>
</table>
$sec$, 'section', 6),

('tok_family', '{{family_nav}} token', '{{family_nav}}
', 'section', 7),

('tok_era_nav', '{{era_nav}} token', '{{era_nav}}
', 'section', 8),

('tok_era_hub_link', '{{era_hub_link}} token', '{{era_hub_link}}
', 'section', 9),

('tok_era_index', '{{era_index}} token', '{{era_index}}
', 'section', 10)

ON CONFLICT (key) DO NOTHING;

-- Layouts: outer rounded/bordered wrapper + the sections above,
-- concatenated in order (literal copy, not runtime section-references —
-- editing a section afterward does NOT retroactively change an already-
-- composed layout; that's a known limitation of "compose once", not a
-- bug).
INSERT INTO description_sections (key, label, html, kind, sort_order) VALUES

('deep_sea_spoke', 'Deep Sea — Spoke',
$lay$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;border:1px solid #1d4d7a;border-radius:14px;overflow:hidden;">
<tr><td>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0a1f38,#123a63);">
<tr><td style="padding:24px;text-align:center;border-bottom:1px solid #1d4d7a;">
<span style="display:inline-block;width:56px;height:56px;border-radius:50%;background:#3fc3e8;box-shadow:0 0 18px rgba(63,195,232,0.6);"></span>
<p style="margin:12px 0 0;font-size:22px;font-weight:900;color:#fff;letter-spacing:0.06em;font-family:sans-serif;">BIGGYFISH <span style="color:#3fc3e8;">CARD SHOP</span></p>
<p style="margin:6px 0 0;font-size:11px;color:#7fa8c9;letter-spacing:0.05em;font-family:sans-serif;">100% AUTHENTIC &middot; 88K+ SOLD &middot; 100% POSITIVE</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:26px 24px 6px;text-align:center;">
<span style="display:inline-block;border:1px solid #3fc3e8;color:#3fc3e8;font-size:11px;font-weight:bold;letter-spacing:0.14em;padding:4px 14px;border-radius:14px;font-family:sans-serif;">{{series_name}}</span>
<p style="margin:12px 0 0;font-size:30px;font-weight:900;color:#fff;line-height:1.05;font-family:sans-serif;">{{set_name}}</p>
<p style="margin:4px 0 0;font-size:15px;color:#9db2c6;font-family:sans-serif;">Singles</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:22px 24px 6px;">
<p style="margin:0 0 12px;font-size:14.5px;line-height:1.6;color:#c3d4e3;text-align:center;font-family:sans-serif;">Take your pick from the drop-down above &mdash; all cards <b style="color:#fff;">NM&ndash;M (Pack Fresh)</b>.</p>
<table role="presentation" width="100%" bgcolor="#12365c" cellpadding="0" cellspacing="0" style="background:rgba(63,195,232,0.1);border:1px solid #1d4d7a;border-radius:9px;">
<tr><td style="padding:12px 14px;text-align:center;"><p style="margin:0;font-size:13px;color:#9fe0f2;font-family:sans-serif;">&#10003; Cards shown with front &amp; back images are the exact card you'll receive.</p></td></tr>
</table>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;"><tr><td style="padding:0 24px;">
{{family_nav}}
{{era_hub_link}}
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:22px 24px 0;">
<p style="margin:0 0 10px;font-size:11px;font-weight:bold;color:#7fa8c9;text-transform:uppercase;letter-spacing:0.08em;font-family:sans-serif;">Rarities you'll find</p>
<p style="margin:0;line-height:2;">
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Full Art</span>
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Alternate Art</span>
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Illustration Rare</span>
</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;"><tr><td style="padding:0 24px;">
{{era_index}}
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:24px 24px 22px;border-top:1px solid #1d4d7a;">
<p style="margin:0 0 6px;font-size:14px;font-weight:bold;color:#fff;font-family:sans-serif;">Questions? International or shipping?</p>
<p style="margin:0 0 10px;font-size:13px;color:#9db2c6;line-height:1.6;font-family:sans-serif;">Reach out anytime &mdash; I'll take good care of you.</p>
<p style="margin:0;font-size:13px;color:#9db2c6;line-height:1.6;font-family:sans-serif;"><b style="color:#c3d4e3;">Packaging:</b> penny sleeve + top loader, PWE with tracking. Combined shipping on multiples.</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#e8467e" cellpadding="0" cellspacing="0" style="background:#e8467e;">
<tr><td style="padding:14px;text-align:center;"><a href="#" style="font-size:13px;font-weight:900;color:#fff;text-decoration:none;letter-spacing:0.04em;font-family:sans-serif;">VISIT OUR STORE &rsaquo;</a></td></tr>
</table>
</td></tr></table>
$lay$, 'layout', 3),

('deep_sea_hub', 'Deep Sea — Hub',
$lay$<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;border:1px solid #1d4d7a;border-radius:14px;overflow:hidden;">
<tr><td>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0a1f38,#123a63);">
<tr><td style="padding:24px;text-align:center;border-bottom:1px solid #1d4d7a;">
<span style="display:inline-block;width:56px;height:56px;border-radius:50%;background:#3fc3e8;box-shadow:0 0 18px rgba(63,195,232,0.6);"></span>
<p style="margin:12px 0 0;font-size:22px;font-weight:900;color:#fff;letter-spacing:0.06em;font-family:sans-serif;">BIGGYFISH <span style="color:#3fc3e8;">CARD SHOP</span></p>
<p style="margin:6px 0 0;font-size:11px;color:#7fa8c9;letter-spacing:0.05em;font-family:sans-serif;">100% AUTHENTIC &middot; 88K+ SOLD &middot; 100% POSITIVE</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:26px 24px 6px;text-align:center;">
<span style="display:inline-block;border:1px solid #3fc3e8;color:#3fc3e8;font-size:11px;font-weight:bold;letter-spacing:0.14em;padding:4px 14px;border-radius:14px;font-family:sans-serif;">{{series_name}}</span>
<p style="margin:12px 0 0;font-size:30px;font-weight:900;color:#fff;line-height:1.05;font-family:sans-serif;">{{set_name}}</p>
<p style="margin:4px 0 0;font-size:15px;color:#9db2c6;font-family:sans-serif;">Era hub &mdash; shop every set</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:22px 24px 6px;">
<p style="margin:0 0 12px;font-size:14.5px;line-height:1.6;color:#c3d4e3;text-align:center;font-family:sans-serif;">Take your pick from the drop-down above &mdash; all cards <b style="color:#fff;">NM&ndash;M (Pack Fresh)</b>.</p>
<table role="presentation" width="100%" bgcolor="#12365c" cellpadding="0" cellspacing="0" style="background:rgba(63,195,232,0.1);border:1px solid #1d4d7a;border-radius:9px;">
<tr><td style="padding:12px 14px;text-align:center;"><p style="margin:0;font-size:13px;color:#9fe0f2;font-family:sans-serif;">&#10003; Cards shown with front &amp; back images are the exact card you'll receive.</p></td></tr>
</table>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;"><tr><td style="padding:0 24px;">
{{family_nav}}
{{era_nav}}
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:22px 24px 0;">
<p style="margin:0 0 10px;font-size:11px;font-weight:bold;color:#7fa8c9;text-transform:uppercase;letter-spacing:0.08em;font-family:sans-serif;">Rarities you'll find</p>
<p style="margin:0;line-height:2;">
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Full Art</span>
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Alternate Art</span>
<span style="display:inline-block;border:1px solid #1d4d7a;color:#9db2c6;border-radius:6px;padding:4px 10px;font-size:11px;margin:0 5px 5px 0;font-family:sans-serif;">Illustration Rare</span>
</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;"><tr><td style="padding:0 24px;">
{{era_index}}
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#0a1f38" cellpadding="0" cellspacing="0" style="background:#0a1f38;">
<tr><td style="padding:24px 24px 22px;border-top:1px solid #1d4d7a;">
<p style="margin:0 0 6px;font-size:14px;font-weight:bold;color:#fff;font-family:sans-serif;">Questions? International or shipping?</p>
<p style="margin:0 0 10px;font-size:13px;color:#9db2c6;line-height:1.6;font-family:sans-serif;">Reach out anytime &mdash; I'll take good care of you.</p>
<p style="margin:0;font-size:13px;color:#9db2c6;line-height:1.6;font-family:sans-serif;"><b style="color:#c3d4e3;">Packaging:</b> penny sleeve + top loader, PWE with tracking. Combined shipping on multiples.</p>
</td></tr></table>
<table role="presentation" width="100%" bgcolor="#e8467e" cellpadding="0" cellspacing="0" style="background:#e8467e;">
<tr><td style="padding:14px;text-align:center;"><a href="#" style="font-size:13px;font-weight:900;color:#fff;text-decoration:none;letter-spacing:0.04em;font-family:sans-serif;">VISIT OUR STORE &rsaquo;</a></td></tr>
</table>
</td></tr></table>
$lay$, 'layout', 4)

ON CONFLICT (key) DO NOTHING;

COMMIT;
