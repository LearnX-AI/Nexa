<?php
namespace local_nexa_integration;

defined('MOODLE_INTERNAL') || die();

/**
 * Builds the Nexa launcher markup that is injected before the page footer.
 */
class helper {

    /** @var bool Guard so the launcher is only ever emitted once per request. */
    protected static $rendered = false;

    /**
     * Returns the launcher HTML, or an empty string if it should not be shown.
     *
     * @return string
     */
    public static function get_launcher_html(): string {
        global $USER;

        if (self::$rendered) {
            return '';
        }

        if (empty($USER->email) || !get_config('local_nexa_integration', 'show_block')) {
            return '';
        }

        $base = trim((string) get_config('local_nexa_integration', 'nexa_base_url'));
        if ($base === '') {
            return '';
        }

        $bubble = (int) get_config('local_nexa_integration', 'bubble_size');
        if ($bubble < 40 || $bubble > 400) {
            $bubble = 96;
        }

        $offset = (int) get_config('local_nexa_integration', 'edge_offset');
        if ($offset < 0 || $offset > 200) {
            $offset = 16;
        }

        $url = rtrim($base, '/')
            . '/?email=' . rawurlencode($USER->email)
            . '&name=' . rawurlencode(fullname($USER));

        // Safe JavaScript string literal.
        $jsurl = json_encode($url, JSON_UNESCAPED_SLASHES | JSON_HEX_TAG | JSON_HEX_AMP
            | JSON_HEX_APOS | JSON_HEX_QUOT);

        // Only the origin is accepted for postMessage traffic.
        $origin = self::origin_of($base);
        $jsorigin = json_encode($origin, JSON_UNESCAPED_SLASHES | JSON_HEX_TAG | JSON_HEX_AMP
            | JSON_HEX_APOS | JSON_HEX_QUOT);

        self::$rendered = true;

        return self::render($jsurl, $jsorigin, $bubble, $offset);
    }

    /**
     * Extracts scheme://host[:port] from a URL.
     *
     * @param string $url
     * @return string
     */
    protected static function origin_of(string $url): string {
        $parts = parse_url($url);
        if (empty($parts['scheme']) || empty($parts['host'])) {
            return '';
        }
        $origin = $parts['scheme'] . '://' . $parts['host'];
        if (!empty($parts['port'])) {
            $origin .= ':' . $parts['port'];
        }
        return $origin;
    }

    /**
     * @param string $jsurl JSON-encoded iframe src.
     * @param string $jsorigin JSON-encoded expected message origin.
     * @param int $bubble Collapsed launcher size in pixels.
     * @param int $offset Distance from the viewport corner in pixels.
     * @return string
     */
    protected static function render(string $jsurl, string $jsorigin, int $bubble, int $offset): string {
        $b = $bubble;
        $o = $offset;

        return <<<HTML
<style id="nexa-moodle-style">
#nexa-moodle-iframe {
    position: fixed !important;
    right: {$o}px;
    bottom: {$o}px;
    left: auto !important;
    top: auto !important;
    display: block !important;
    border: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    background: transparent !important;
    background-color: transparent !important;
    color-scheme: normal;
    box-shadow: none !important;
    overflow: hidden;
    border-radius: 18px;
    z-index: 2147483000 !important;
    /* Collapsed footprint = launcher bubble only. Anything larger sits on top
       of the page and eats clicks meant for Moodle. */
    width: {$b}px;
    height: {$b}px;
    transition: width .25s ease, height .25s ease, opacity .2s ease;
}
#nexa-moodle-iframe[data-nexa-open="1"] {
    border-radius: 12px;
}
#nexa-moodle-iframe[data-nexa-fullscreen="1"] {
    right: 0 !important;
    bottom: 0 !important;
    border-radius: 0;
}
/* Dismissed: no rectangle at all, so nothing can intercept pointer events. */
#nexa-moodle-iframe[data-nexa-hidden="1"] {
    width: 0 !important;
    height: 0 !important;
    opacity: 0 !important;
    visibility: hidden !important;
    pointer-events: none !important;
}
@media print {
    #nexa-moodle-iframe, #nexa-moodle-style { display: none !important; }
}
</style>
<script>
(function () {
    var SRC = {$jsurl};
    var ORIGIN = {$jsorigin};
    var BUBBLE = {$b};
    var OFFSET = {$o};

    function init() {
        if (document.getElementById("nexa-moodle-iframe")) {
            return;
        }
        if (window.self !== window.top) {
            return; // Never nest the launcher inside another frame.
        }

        var iframe = document.createElement("iframe");
        iframe.id = "nexa-moodle-iframe";
        iframe.title = "Nexa AI Assistant";
        iframe.src = SRC;
        iframe.allow = "clipboard-write; clipboard-read";
        iframe.setAttribute("scrolling", "no");
        iframe.setAttribute("data-nexa-open", "0");
        iframe.setAttribute("data-nexa-hidden", "0");
        iframe.setAttribute("data-nexa-fullscreen", "0");
        document.body.appendChild(iframe);

        function isMobile() {
            return window.innerWidth < 500;
        }

        function collapse() {
            iframe.setAttribute("data-nexa-open", "0");
            iframe.setAttribute("data-nexa-fullscreen", "0");
            iframe.setAttribute("data-nexa-hidden", "0");
            iframe.style.width  = BUBBLE + "px";
            iframe.style.height = BUBBLE + "px";
            iframe.style.right  = OFFSET + "px";
            iframe.style.bottom = OFFSET + "px";
        }

        function setSize(open, expanded, history) {
            var mobile = isMobile();

            iframe.setAttribute("data-nexa-hidden", "0");

            if (!open && !history) {
                collapse();
                return;
            }

            iframe.setAttribute("data-nexa-open", "1");
            iframe.setAttribute("data-nexa-fullscreen", mobile ? "1" : "0");

            if (history) {
                iframe.style.width  = mobile ? "100vw"  : "840px";
                iframe.style.height = mobile ? "100dvh" : "620px";
            } else if (mobile) {
                iframe.style.width  = "100vw";
                iframe.style.height = "100dvh";
            } else {
                iframe.style.width  = expanded ? "880px" : "500px";
                iframe.style.height = "720px";
            }

            if (!mobile) {
                iframe.style.right  = OFFSET + "px";
                iframe.style.bottom = OFFSET + "px";
            }
        }

        function setHidden(hidden) {
            if (hidden) {
                iframe.setAttribute("data-nexa-hidden", "1");
            } else {
                collapse();
            }
        }

        var warned = false;
        window.addEventListener("message", function (e) {
            if (ORIGIN && e.origin !== ORIGIN) {
                if (e.data && e.data.nexa && !warned) {
                    warned = true;
                    window.console && console.warn(
                        "[Nexa] Ignoring message from " + e.origin +
                        " - expected " + ORIGIN +
                        ". Update the Nexa Chat Base URL setting to match.");
                }
                return;
            }
            var d = e.data || {};
            if (!d.nexa) {
                return;
            }
            if (d.action === "resize") {
                setSize(d.open, d.expanded, d.history);
            } else if (d.action === "hide" || d.action === "dismiss") {
                setHidden(true);
            } else if (d.action === "show") {
                setHidden(false);
            }
        });

        window.addEventListener("resize", function () {
            if (iframe.getAttribute("data-nexa-hidden") === "1") {
                return;
            }
            if (iframe.getAttribute("data-nexa-open") !== "1") {
                collapse();
            } else {
                setSize(true, iframe.style.width === "880px", false);
            }
        });

        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape" && iframe.getAttribute("data-nexa-open") === "1") {
                collapse();
            }
        });

        collapse();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
</script>
HTML;
    }
}
