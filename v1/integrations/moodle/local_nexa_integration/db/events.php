<?php
defined('MOODLE_INTERNAL') || die();

$observers = [
    [
        'eventname' => '\core\event\user_loggedin',
        'callback'  => 'local_nexa_integration\observer::user_loggedin',
    ],
];
