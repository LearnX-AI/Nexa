<?php
// This file is part of the local_nexa_integration plugin.

defined('MOODLE_INTERNAL') || die();

function xmldb_local_nexa_integration_upgrade($oldversion) {
    global $DB;
    $dbman = $DB->get_manager();

    if ($oldversion < 2026111000) {
        // 2.0: knowledge sync to NEXA v1 (NEXA-M2-007).
        foreach (['local_nexa_outbox', 'local_nexa_items'] as $name) {
            if (!$dbman->table_exists($name)) {
                $dbman->install_one_table_from_xmldb_file(__DIR__ . '/install.xml', $name);
            }
        }
        upgrade_plugin_savepoint(true, 2026111000, 'local', 'nexa_integration');
    }
    return true;
}
