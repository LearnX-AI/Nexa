<?php
namespace local_nexa_integration;

defined('MOODLE_INTERNAL') || die();

class observer {

    /**
     * Handles \core\event\user_loggedin.
     *
     * @param \core\event\user_loggedin $event
     */
    public static function user_loggedin(\core\event\user_loggedin $event) {
        global $USER;

        if (empty($USER->email)) {
            return;
        }

        $fullname = fullname($USER);

        if (get_config('local_nexa_integration', 'welcome_notice')) {
            \core\notification::add(
                get_string('welcome', 'local_nexa_integration', $fullname),
                \core\notification::SUCCESS
            );
        }

        // Queue the backend ping as an adhoc task. Doing this inline blocks the
        // login response until Nexa answers (or times out), which stalls every
        // user whenever the Nexa host is slow or down.
        if (get_config('local_nexa_integration', 'nexa_base_url')) {
            $task = new \local_nexa_integration\task\notify_nexa();
            $task->set_custom_data([
                'email' => $USER->email,
                'name'  => $fullname,
            ]);
            \core\task\manager::queue_adhoc_task($task, true);
        }
    }
}
