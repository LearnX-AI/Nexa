<?php
// This file is part of the local_nexa_integration plugin.

namespace local_nexa_integration\task;

use local_nexa_integration\sync\client;
use local_nexa_integration\sync\client_exception;
use local_nexa_integration\sync\collector;
use local_nexa_integration\sync\outbox;

defined('MOODLE_INTERNAL') || die();

/**
 * Nightly safety net for knowledge sync (NEXA-M2-007).
 *
 * Event observers queue changes as they happen; this task catches anything
 * they missed (changes made while the plugin was off, bulk restores, direct
 * database edits):
 *
 * 1. Scan every course's supported activities and queue an upsert for any
 *    whose fingerprint (content revision, visibility, approval, title,
 *    curriculum, scope) differs from what NEXA was last told.
 * 2. Queue a delete for every item NEXA holds that no longer exists.
 * 3. Reconcile each course with NEXA: NEXA reports items it is missing or
 *    holds at an older revision (queued again here), and withdraws items
 *    Moodle no longer has.
 */
class sync_materials extends \core\task\scheduled_task {

    public function get_name() {
        return get_string('syncmaterialstask', 'local_nexa_integration');
    }

    public function execute() {
        global $DB;
        if (!collector::enabled()) {
            mtrace('nexa sync: not configured, skipping.');
            return;
        }
        $known = [];
        foreach ($DB->get_records(outbox::ITEMS) as $it) {
            $known[$it->itemid] = $it;
        }
        $client = new client();
        $site = collector::site();
        $queued = $deleted = 0;
        foreach (get_courses('all', 'c.id ASC', 'c.id') as $course) {
            if ($course->id == SITEID) {
                continue;
            }
            $present = [];
            try {
                $modinfo = get_fast_modinfo($course->id);
            } catch (\Throwable $e) {
                mtrace("nexa sync: course {$course->id}: " . $e->getMessage());
                continue;
            }
            foreach ($modinfo->get_cms() as $cm) {
                if (!in_array($cm->modname, collector::KINDS, true) || !empty($cm->deletioninprogress)) {
                    continue;
                }
                $itemid = collector::item_id($cm->id);
                $facts = collector::facts($course->id, $cm->id);
                $content = $facts ? collector::content($cm) : null;
                if (!$facts || !$content || empty($facts['curriculum']['grade_levels'])) {
                    if (isset($known[$itemid]) && $known[$itemid]->state === 'sent') {
                        outbox::delete((string) $course->id, $cm->id);
                        $deleted++;
                    }
                    continue;
                }
                $present[$itemid] = $content['revision'];
                $fp = collector::fingerprint($facts, $content);
                $k = $known[$itemid] ?? null;
                if (!$k || $k->fingerprint !== $fp || !in_array($k->state, ['sent', 'rejected'], true)) {
                    if ($this->not_already_queued($itemid, $fp)) {
                        outbox::upsert($course->id, $cm->id);
                        $queued++;
                    }
                }
            }
            foreach ($known as $itemid => $k) {
                if ((string) $k->courseid === (string) $course->id && $k->state === 'sent' && !isset($present[$itemid])) {
                    outbox::delete((string) $course->id, (int) substr($itemid, 2));
                    $deleted++;
                }
            }
            if ($present || $this->course_known($known, (string) $course->id)) {
                $this->reconcile($client, $site, (string) $course->id, $present);
            }
        }
        mtrace("nexa sync: scan queued={$queued} deletes={$deleted}");
    }

    protected function not_already_queued(string $itemid, string $fp): bool {
        global $DB;
        return !$DB->record_exists(outbox::TABLE, ['itemid' => $itemid, 'status' => 'pending', 'fingerprint' => $fp]);
    }

    protected function course_known(array $known, string $courseid): bool {
        foreach ($known as $k) {
            if ((string) $k->courseid === $courseid && $k->state === 'sent') {
                return true;
            }
        }
        return false;
    }

    protected function reconcile(client $client, array $site, string $courseid, array $present): void {
        $items = [];
        foreach ($present as $itemid => $rev) {
            $items[] = ['item_id' => $itemid, 'revision' => (string) $rev];
        }
        try {
            $r = $client->reconcile($site, $courseid, $items, true);
        } catch (client_exception $e) {
            mtrace("nexa sync: reconcile course {$courseid} failed: " . $e->getMessage());
            return;
        }
        foreach (array_merge($r['missing_in_nexa'] ?? [], $r['revision_mismatch'] ?? []) as $itemid) {
            if ($this->not_queued_at_all($itemid)) {
                outbox::upsert((int) $courseid, (int) substr($itemid, 2));
            }
        }
        if (!empty($r['withdrawn'])) {
            mtrace("nexa sync: NEXA withdrew from course {$courseid}: " . implode(', ', $r['withdrawn']));
        }
    }

    protected function not_queued_at_all(string $itemid): bool {
        global $DB;
        return !$DB->record_exists(outbox::TABLE, ['itemid' => $itemid, 'status' => 'pending']);
    }
}
