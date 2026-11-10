<?php
defined('MOODLE_INTERNAL') || die();

$tasks = [
    [
        // Send queued changes to NEXA. Cheap when the queue is empty.
        'classname' => 'local_nexa_integration\task\push_outbox',
        'blocking'  => 0,
        'minute'    => '*/5',
        'hour'      => '*',
        'day'       => '*',
        'month'     => '*',
        'dayofweek' => '*',
    ],
    [
        // Nightly safety net: scan, queue anything missed, reconcile with NEXA.
        'classname' => 'local_nexa_integration\task\sync_materials',
        'blocking'  => 0,
        'minute'    => '15',
        'hour'      => '2',
        'day'       => '*',
        'month'     => '*',
        'dayofweek' => '*',
    ],
    [
        // LEGACY: feeds the v0 chat's /ingest-material. Off unless enabled.
        'classname' => 'local_nexa_integration\task\legacy_v0_push',
        'blocking'  => 0,
        'minute'    => '45',
        'hour'      => '2',
        'day'       => '*',
        'month'     => '*',
        'dayofweek' => '*',
        'disabled'  => 1,
    ],
];
