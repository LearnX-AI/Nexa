<?php
// Moodle 4.4+ hook registration. Ignored on older versions, which use the
// legacy local_nexa_integration_before_footer() callback in lib.php instead.

defined('MOODLE_INTERNAL') || die();

$callbacks = [
    [
        'hook'     => \core\hook\output\before_footer_html_generation::class,
        'callback' => 'local_nexa_integration\hook_callbacks::before_footer_html_generation',
        'priority' => 0,
    ],
];
