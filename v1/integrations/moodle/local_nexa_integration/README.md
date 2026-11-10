# Nexa Chatbot Integration (local_nexa_integration) v1.1

Embeds the Nexa AI assistant into Moodle as a floating launcher.

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

## Install

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

The plugin transmits the logged-in user's email address and full name to the configured
external Nexa service, both as iframe query parameters and via the login notification task.
