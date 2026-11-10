<?php
// This file is part of the local_nexa_integration plugin.

namespace local_nexa_integration\privacy;

use core_privacy\local\metadata\collection;

defined('MOODLE_INTERNAL') || die();

/**
 * The plugin stores no personal data. The chat launcher sends the user's
 * email and name to the Nexa chat; knowledge sync sends course content only.
 */
class provider implements \core_privacy\local\metadata\provider {

    public static function get_metadata(collection $collection): collection {
        $collection->add_external_location_link('nexa_chat', [
            'email' => 'privacy:metadata:nexa_chat:email',
            'fullname' => 'privacy:metadata:nexa_chat:fullname',
        ], 'privacy:metadata:nexa_chat');
        $collection->add_external_location_link('nexa_knowledge', [
            'content' => 'privacy:metadata:nexa_knowledge:content',
        ], 'privacy:metadata:nexa_knowledge');
        return $collection;
    }
}
