<?php
defined('MOODLE_INTERNAL') || die();

/**
 * Legacy before-footer callback for Moodle 4.0 - 4.3.
 *
 * On Moodle 4.4+ the same markup is emitted through
 * \local_nexa_integration\hook_callbacks::before_footer_html_generation().
 * A static guard inside the helper makes sure it is never output twice.
 */
function local_nexa_integration_before_footer() {
    echo \local_nexa_integration\helper::get_launcher_html();
}
