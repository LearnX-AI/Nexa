<?php
namespace local_nexa_integration;

defined('MOODLE_INTERNAL') || die();

class observer {

    // ---------------------------------------------------------------- knowledge sync (NEXA-M2-007)
    //
    // Observers only queue: nothing here talks to NEXA, so a slow or offline
    // NEXA never slows a teacher down. push_outbox sends the queue.

    protected static function kind_ok(?string $modname): bool {
        return $modname !== null && in_array($modname, \local_nexa_integration\sync\collector::KINDS, true);
    }

    protected static function safely(callable $fn): void {
        try {
            if (\local_nexa_integration\sync\collector::enabled()) {
                $fn();
            }
        } catch (\Throwable $e) {
            // Never break the teacher's save. The nightly scan catches up.
            debugging('local_nexa_integration: queueing failed: ' . $e->getMessage(), DEBUG_NORMAL);
        }
    }

    /** \core\event\course_module_created and \core\event\course_module_updated */
    public static function course_module_changed(\core\event\base $event) {
        $other = $event->other;
        if (!self::kind_ok($other['modulename'] ?? null)) {
            return;
        }
        self::safely(function() use ($event) {
            \local_nexa_integration\sync\outbox::upsert((int) $event->courseid, (int) $event->objectid,
                (int) $event->timecreated);
        });
    }

    /** \core\event\course_module_deleted */
    public static function course_module_deleted(\core\event\course_module_deleted $event) {
        if (!self::kind_ok($event->other['modulename'] ?? null)) {
            return;
        }
        self::safely(function() use ($event) {
            \local_nexa_integration\sync\outbox::delete((string) $event->courseid, (int) $event->objectid,
                (int) $event->timecreated);
        });
    }

    /** \core\event\course_updated: visibility, name or category may have changed. */
    public static function course_updated(\core\event\course_updated $event) {
        self::safely(function() use ($event) {
            foreach (get_fast_modinfo($event->courseid)->get_cms() as $cm) {
                if (self::kind_ok($cm->modname)) {
                    \local_nexa_integration\sync\outbox::upsert((int) $event->courseid, (int) $cm->id,
                        (int) $event->timecreated);
                }
            }
        });
    }

    /** \core\event\course_deleted: its activities are gone without their own events. */
    public static function course_deleted(\core\event\course_deleted $event) {
        global $DB;
        self::safely(function() use ($event, $DB) {
            foreach ($DB->get_records(\local_nexa_integration\sync\outbox::ITEMS,
                    ['courseid' => (string) $event->objectid, 'state' => 'sent']) as $it) {
                \local_nexa_integration\sync\outbox::delete((string) $event->objectid, (int) substr($it->itemid, 2),
                    (int) $event->timecreated);
            }
        });
    }

    /** \core\event\tag_added and \core\event\tag_removed: the approval tag. */
    public static function tag_changed(\core\event\base $event) {
        $other = $event->other;
        if (($other['itemtype'] ?? '') !== 'course_modules' || empty($other['itemid'])) {
            return;
        }
        $approvaltag = \core_text::strtolower((string) (get_config('local_nexa_integration', 'approval_tag')
            ?: 'nexa-approved'));
        if (\core_text::strtolower((string) ($other['tagrawname'] ?? $other['tagname'] ?? '')) !== $approvaltag) {
            return;
        }
        self::safely(function() use ($other, $event) {
            global $DB;
            $courseid = $DB->get_field('course_modules', 'course', ['id' => (int) $other['itemid']]);
            if ($courseid) {
                \local_nexa_integration\sync\outbox::upsert((int) $courseid, (int) $other['itemid'],
                    (int) $event->timecreated);
            }
        });
    }

    // ---------------------------------------------------------------- chat launcher

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
