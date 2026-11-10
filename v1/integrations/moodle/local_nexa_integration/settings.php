<?php
defined('MOODLE_INTERNAL') || die();

if (!$ADMIN->fulltree) {
    return;
}

$settings = new admin_settingpage(
    'local_nexa_integration',
    get_string('pluginname', 'local_nexa_integration')
);

$settings->add(new admin_setting_configtext(
    'local_nexa_integration/nexa_base_url',
    get_string('nexa_base_url', 'local_nexa_integration'),
    get_string('nexa_base_url_desc', 'local_nexa_integration'),
    '',
    PARAM_URL
));

$settings->add(new admin_setting_configcheckbox(
    'local_nexa_integration/show_block',
    get_string('show_block', 'local_nexa_integration'),
    get_string('show_block_desc', 'local_nexa_integration'),
    1
));

$settings->add(new admin_setting_configtext(
    'local_nexa_integration/bubble_size',
    get_string('bubble_size', 'local_nexa_integration'),
    get_string('bubble_size_desc', 'local_nexa_integration'),
    96,
    PARAM_INT
));

$settings->add(new admin_setting_configtext(
    'local_nexa_integration/edge_offset',
    get_string('edge_offset', 'local_nexa_integration'),
    get_string('edge_offset_desc', 'local_nexa_integration'),
    16,
    PARAM_INT
));

$settings->add(new admin_setting_configcheckbox(
    'local_nexa_integration/welcome_notice',
    get_string('welcome_notice', 'local_nexa_integration'),
    get_string('welcome_notice_desc', 'local_nexa_integration'),
    0
));

$settings->add(new admin_setting_configpasswordunmask(
    'local_nexa_integration/ingest_token',
    get_string('ingest_token', 'local_nexa_integration'),
    get_string('ingest_token_desc', 'local_nexa_integration'),
    ''
));

$ADMIN->add('localplugins', $settings);
