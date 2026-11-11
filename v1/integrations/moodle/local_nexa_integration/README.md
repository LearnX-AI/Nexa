# Nexa Chatbot Integration (local_nexa_integration) v2.0

Embeds the Nexa AI assistant into Moodle as a floating launcher and, since
2.0, keeps the NEXA Knowledge Service in step with approved course content, so
Nexa answers from the school's own material, with citations
(NEXA-M2-007, contract `nexa.lms-sync/1.0`).

## What changed in 2.0: knowledge sync

1.x pushed every course file, nightly, to the v0 chat's `/ingest-material`.
It sent hidden and restricted files too (so answer keys became answerable),
never removed anything that was deleted in Moodle, sent a file shared by two
courses only once, and kept its "already sent" list in one ever-growing
config value. 2.0 replaces it:

| | 1.x | 2.0 |
|---|---|---|
| Target | v0 chat `/ingest-material` | NEXA v1 Knowledge Service, `nexa.lms-sync/1.0` |
| What | resource files | pages, labels, books, and PDF, DOCX, PPTX files, in File (`resource`) and, since 2.0.1, EduNex lesson presentation (`presentationview`) activities |
| When | nightly | as it happens (event observers), plus a nightly scan and reconcile |
| Approval | none: everything served | only activities tagged `nexa-approved`, or in an auto-approved category, are served; others are sent as pending review |
| Hidden or restricted | sent, served to students | sent as teacher-only |
| Deleted in Moodle | stayed in Nexa forever | withdrawn in NEXA (history kept) |
| Offline or NEXA down | changes lost until the next night | queued in `local_nexa_outbox`, retried with backoff, never lost |
| Who can see it in NEXA | everyone | this school's users only (school scope), unless the site has a national token |
| Auth | token in a form field | per-site sync token in the `Authorization` header |

The 1.x push still exists as the **legacy_v0_push** task for the v0 chat,
**disabled by default**, and now sends only activities students can see.

### Setting up knowledge sync

1. Ask the NEXA operators for a sync token for this site. They mint it with:
   `python -m v1.identity.cli --subject lms:<site-id> --roles lms_sync --lms-site <site-id> --school-id <school-id> --ttl 31536000`
2. Site administration > Plugins > Local plugins > Nexa Chatbot Integration > Knowledge sync:
   NEXA Knowledge Service URL, sync token, site ID and school ID (both must
   match the token), scope, approval tag or auto-approved categories.
3. If NEXA is on a private address, allow it under Site administration >
   General > Security > HTTP security (cURL blocked hosts and allowed ports);
   Moodle blocks private addresses by default.
4. Tick **Enable knowledge sync**. Run the **NEXA knowledge sync: nightly scan
   and reconcile** task once (Scheduled tasks > Run now) to send existing content.
5. Mark content for Nexa by adding the tag `nexa-approved` to an activity
   (Edit settings > Tags), or list whole categories as auto-approved.

Subject and grade come from the course custom fields `nexa_subject` and
`nexa_grade` when they exist, otherwise from the course name ("Grade 12
Biology"). Activities in a course with no grade are not sent (NEXA needs the
grade to filter); the reason is recorded in `local_nexa_items.detail`.

### Monitoring

- Moodle: `local_nexa_outbox` (`status` pending, sent, rejected; `lasterror`), the task logs.
- NEXA: `GET /v1/knowledge/lms/status` with the site's token (counts, recent
  failures), and `GET /v1/knowledge/lms/events?state=failed`.

### Tests

- `php tests/contract_check.php` checks the mapping rules without Moodle; NEXA's
  test suite (`v1/tests/test_moodle_contract.py`) feeds its output through NEXA's
  own contract model and sync endpoint.
- `tests/mapper_test.php` is a Moodle PHPUnit test of the same rules.

## Chat launcher (1.1)

## What changed in 1.1

**Fixes the white box that blocked clicks on the page behind Nexa.**

- The collapsed iframe was a permanent **240x170 px** rectangle at `z-index: 2147483000`.
  An iframe is a solid hit-target across its whole rectangle regardless of what it paints,
  so that area swallowed clicks meant for Moodle's footer and navigation. `setSize()` never
  shrank it below 240x170, so closing Nexa did not help.
  The collapsed footprint is now **96x96** (configurable) and a real dismissed state
  (`width/height: 0; visibility: hidden; pointer-events: none`) is supported.
- Removed `allowtransparency="true"` - it is a dead IE-only attribute. Transparency now
  relies on the iframe element being transparent **plus** the Nexa app not painting an
  opaque `body` background (see "Nexa frontend changes" below).
- All geometry moved into an `!important` stylesheet so Moodle themes cannot override it.
- `postMessage` handler now validates `event.origin` against the configured base URL, and
  logs a console warning on mismatch instead of failing silently.
- Escape key collapses the panel, as a safety net if the app's own close button breaks.
- The URL is emitted with `json_encode()` instead of `addslashes()` (correct JS escaping).
- Moodle 4.4+ hook support (`\core\hook\output\before_footer_html_generation`) alongside
  the legacy `lib.php` callback, with a static guard so the launcher never renders twice.
- The login ping to the Nexa backend moved from a blocking `file_get_contents()` in the
  event observer to an adhoc task. Previously every login stalled up to 3 seconds whenever
  Nexa was slow or unreachable.
- The "Welcome, name" login notification is now opt-in (off by default).

## Install (both features)

1. Copy the `nexa_integration` folder to `<moodleroot>/local/nexa_integration`.
   On this server that is `/var/www/html/moodle2/public/local/nexa_integration`.
2. `sudo chown -R www-data:www-data /var/www/html/moodle2/public/local/nexa_integration`
3. Visit **Site administration > Notifications** and complete the upgrade, or run:
   `sudo -u www-data php admin/cli/upgrade.php`
4. Purge caches: `sudo -u www-data php admin/cli/purge_caches.php`
5. Configure at **Site administration > Plugins > Local plugins > Nexa Chatbot Integration**.
6. Hard-refresh the browser (Ctrl+Shift+R) - the old inline script is cached in page HTML.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Nexa Chat Base URL | *(empty)* | Nothing renders until this is set. Must match the origin Nexa posts from. |
| Show Nexa Chat launcher | On | Master switch. |
| Collapsed launcher size (px) | 96 | Keep this just big enough for the bubble. Every extra pixel is dead area over the page. |
| Edge offset (px) | 16 | Gap from the bottom-right corner. |
| Show welcome notification on login | Off | |

## Nexa frontend changes (required)

The plugin controls the iframe rectangle. The **white fill inside it comes from the Nexa app's
own document** and can only be fixed there.

1. Make the app document transparent, and paint the panel on the container instead of `body`:

```css
html, body {
    background: transparent !important;
}
#nexa-app-panel {          /* your actual chat panel element */
    background: #ffffff;
    border-radius: 12px;
}
```

2. Keep telling Moodle about state changes (already implemented):

```js
parent.postMessage({ nexa: true, action: "resize", open: true },  "*");
parent.postMessage({ nexa: true, action: "resize", open: false }, "*");
parent.postMessage({ nexa: true, action: "resize", open: true, expanded: true }, "*");
parent.postMessage({ nexa: true, action: "resize", history: true }, "*");
```

3. New - when the user fully dismisses Nexa, collapse the iframe to nothing:

```js
parent.postMessage({ nexa: true, action: "hide" }, "*");
parent.postMessage({ nexa: true, action: "show" }, "*");   // to bring it back
```

## Verifying the fix

In DevTools, run:

```js
document.getElementById('nexa-moodle-iframe').getBoundingClientRect();
```

While closed this should report roughly 96x96 at the bottom-right. Anything larger means the
`bubble_size` setting is too high, or the app is not sending `open: false` on close.

## Privacy

The chat launcher transmits the logged-in user's email address and full name to the configured
external Nexa chat service, both as iframe query parameters and via the login notification task.
Knowledge sync transmits course content only, never user data.

**Known issue (chat launcher, unchanged in 2.0):** the chat trusts the email in the iframe URL
to decide teacher or student mode, so anyone who edits the URL can claim to be a teacher. The fix
is for Moodle to pass a short-lived signed identity token instead, verified by NEXA's identity
service; it is tracked separately from knowledge sync.
