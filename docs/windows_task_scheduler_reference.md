# Windows Task Scheduler — Card-Board-MasterMind reference

Run from an elevated (Administrator) Command Prompt on the Windows desktop.
Replace `C:\actual\path\to\...` with your real project path.

---

## Registered scheduled tasks (one-time setup, already done)

### EbayPullOrders — every 15 minutes
```
schtasks /create /tn "EbayPullOrders" /tr "C:\actual\path\run_ebay_pull.bat" /sc minute /mo 15 /ru "%USERNAME%"
```

### EbaySyncFees — daily at 4 AM
```
schtasks /create /tn "EbaySyncFees" /tr "C:\actual\path\run_ebay_syncfees.bat" /sc daily /st 04:00 /ru "%USERNAME%"
```

### CBMPickingAPI — on logon (long-running server)
```
schtasks /create /tn "CBMPickingAPI" /tr "C:\actual\path\run_picking_api.bat" /sc onlogon /ru "%USERNAME%"
```

---

## Everyday commands (use these often)

**Manually trigger a task right now** (same as it firing on schedule — useful
to test after any change, or to force an immediate run):
```
schtasks /run /tn "EbayPullOrders"
schtasks /run /tn "EbaySyncFees"
schtasks /run /tn "CBMPickingAPI"
```

**Check a task's status / next run time:**
```
schtasks /query /tn "EbayPullOrders"
schtasks /query /tn "EbaySyncFees"
schtasks /query /tn "CBMPickingAPI"
```

**Check status with full details** (last run result, last run time, etc.):
```
schtasks /query /tn "EbayPullOrders" /fo LIST /v
```

**Check if the picking API process is actually running:**
```
tasklist | findstr python
```

---

## Occasional / maintenance commands

**Disable a task temporarily** (without deleting it):
```
schtasks /change /tn "EbayPullOrders" /disable
```

**Re-enable it:**
```
schtasks /change /tn "EbayPullOrders" /enable
```

**Delete a task entirely** (rare — only if retiring something):
```
schtasks /delete /tn "EbayPullOrders"
```
(Add `/f` to skip the confirmation prompt: `schtasks /delete /tn "EbayPullOrders" /f`)

---

## Related one-time setup (not schtasks, but same context)

**Firewall rule for the Picking API** (needed once, so the Mac can reach
port 8765 over the LAN):
```
netsh advfirewall firewall add rule name="CBM Picking API" dir=in action=allow protocol=TCP localport=8765
```

---

## Known follow-up (not yet done via CLI)

**"Do not start a new instance" setting for EbayPullOrders** — this
prevents overlapping runs if a pull ever takes longer than 15 minutes. This
isn't a plain `schtasks /create` flag; it needs to be set either through
the Task Scheduler GUI (right-click the task -> Properties -> Settings tab)
or via an XML task definition. Worth doing at some point as a defensive
measure, even though the actual crash-loop bug this session turned out to
have a different root cause.
