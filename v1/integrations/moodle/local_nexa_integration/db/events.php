<?php
defined('MOODLE_INTERNAL') || die();

$observers = [
    [
        'eventname' => '\core\event\user_loggedin',
        'callback'  => 'local_nexa_integration\observer::user_loggedin',
    ],
    // Knowledge sync (NEXA-M2-007): queue changes as they happen.
    [
        'eventname' => '\core\event\course_module_created',
        'callback'  => 'local_nexa_integration\observer::course_module_changed',
    ],
    [
        'eventname' => '\core\event\course_module_updated',
        'callback'  => 'local_nexa_integration\observer::course_module_changed',
    ],
    [
        'eventname' => '\core\event\course_module_deleted',
        'callback'  => 'local_nexa_integration\observer::course_module_deleted',
    ],
    [
        'eventname' => '\core\event\course_updated',
        'callback'  => 'local_nexa_integration\observer::course_updated',
    ],
    [
        'eventname' => '\core\event\course_deleted',
        'callback'  => 'local_nexa_integration\observer::course_deleted',
    ],
    [
        'eventname' => '\core\event\tag_added',
        'callback'  => 'local_nexa_integration\observer::tag_changed',
    ],
    [
        'eventname' => '\core\event\tag_removed',
        'callback'  => 'local_nexa_integration\observer::tag_changed',
    ],
];
