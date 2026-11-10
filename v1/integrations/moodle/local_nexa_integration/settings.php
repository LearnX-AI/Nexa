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

// Legacy v0 push (legacy_v0_push task, disabled by default).
$settings->add(new admin_setting_configpasswordunmask(
    'local_nexa_integration/ingest_token',
    get_string('ingest_token', 'local_nexa_integration'),
    get_string('ingest_token_desc', 'local_nexa_integration'),
    ''
));

// ---- Knowledge sync to NEXA v1 (NEXA-M2-007) ----

$settings->add(new admin_setting_heading('local_nexa_integration/syncheading',
    get_string('syncheading', 'local_nexa_integration'),
    get_string('syncheading_desc', 'local_nexa_integration')));

$settings->add(new admin_setting_configcheckbox('local_nexa_integration/sync_enabled',
    get_string('sync_enabled', 'local_nexa_integration'),
    get_string('sync_enabled_desc', 'local_nexa_integration'), 0));

$settings->add(new admin_setting_configtext('local_nexa_integration/knowledge_url',
    get_string('knowledge_url', 'local_nexa_integration'),
    get_string('knowledge_url_desc', 'local_nexa_integration'), '', PARAM_URL));

$settings->add(new admin_setting_configpasswordunmask('local_nexa_integration/sync_token',
    get_string('sync_token', 'local_nexa_integration'),
    get_string('sync_token_desc', 'local_nexa_integration'), ''));

$settings->add(new admin_setting_configtext('local_nexa_integration/site_id',
    get_string('site_id', 'local_nexa_integration'),
    get_string('site_id_desc', 'local_nexa_integration'), '', '/^[a-z0-9][a-z0-9._-]{1,63}$/'));

$settings->add(new admin_setting_configtext('local_nexa_integration/school_id',
    get_string('school_id', 'local_nexa_integration'),
    get_string('school_id_desc', 'local_nexa_integration'), '', PARAM_TEXT));

$settings->add(new admin_setting_configselect('local_nexa_integration/scope',
    get_string('scope', 'local_nexa_integration'),
    get_string('scope_desc', 'local_nexa_integration'), 'school',
    ['school' => get_string('scope_school', 'local_nexa_integration'),
     'national' => get_string('scope_national', 'local_nexa_integration')]));

$settings->add(new admin_setting_configtext('local_nexa_integration/approval_tag',
    get_string('approval_tag', 'local_nexa_integration'),
    get_string('approval_tag_desc', 'local_nexa_integration'), 'nexa-approved', PARAM_TEXT));

$settings->add(new admin_setting_configmulticheckbox('local_nexa_integration/approved_categories',
    get_string('approved_categories', 'local_nexa_integration'),
    get_string('approved_categories_desc', 'local_nexa_integration'), [],
    \core_course_category::make_categories_list()));

$settings->add(new admin_setting_configtext('local_nexa_integration/language',
    get_string('language', 'local_nexa_integration'),
    get_string('language_desc', 'local_nexa_integration'), 'en', '/^[a-z]{2,3}(-[A-Z]{2})?$/'));

$settings->add(new admin_setting_configtext('local_nexa_integration/batch_size',
    get_string('batch_size', 'local_nexa_integration'),
    get_string('batch_size_desc', 'local_nexa_integration'), 50, PARAM_INT));

$ADMIN->add('localplugins', $settings);
