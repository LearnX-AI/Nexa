<?php
defined('MOODLE_INTERNAL') || die();

$tasks = [
    [
        'classname' => 'local_nexa_integration\task\sync_materials',
        // Runs at 02:15 every day. Adjust in Site admin > Server > Scheduled tasks.
        'blocking'  => 0,
        'minute'    => '15',
        'hour'      => '2',
        'day'       => '*',
        'month'     => '*',
        'dayofweek' => '*',
    ],
];
