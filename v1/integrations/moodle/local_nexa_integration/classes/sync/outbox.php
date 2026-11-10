<?php
// This file is part of the local_nexa_integration plugin.
//
// The offline queue. Every change becomes a row here first, with an event id
// generated once. push_outbox sends due rows and keeps them, with backoff,
// until NEXA returns a final answer for that event (accepted, duplicate,
// stale or rejected). Connectivity loss only delays delivery; nothing is lost
// and nothing is sent twice under different ids.

namespace local_nexa_integration\sync;

defined('MOODLE_INTERNAL') || die();

class outbox {

    const TABLE = 'local_nexa_outbox';
    const ITEMS = 'local_nexa_items';

    /**
     * Queue an upsert for an activity. A newer change replaces any queued,
     * unsent change for the same item (only the latest state matters).
     */
    public static function upsert(int $courseid, int $cmid, ?int $when = null): ?int {
        global $DB;
        if (!collector::enabled()) {
            return null;
        }
        $when = $when ?? time();
        $built = collector::build($courseid, $cmid, $when);
        $itemid = collector::item_id($cmid);
        if (isset($built['skip'])) {
            self::note_item($itemid, (string) $courseid, null, null, 'skipped', $built['skip']);
            // A previously sent item that can no longer be sent (for example its
            // file was removed) must not linger in NEXA.
            $prior = $DB->get_record(self::ITEMS, ['itemid' => $itemid]);
            if ($prior && $prior->state === 'sent') {
                return self::delete((string) $courseid, $cmid, $when);
            }
            return null;
        }
        self::drop_pending($itemid);
        return $DB->insert_record(self::TABLE, (object) [
            'eventid' => $built['event']['event_id'], 'itemid' => $itemid, 'courseid' => (string) $courseid,
            'op' => 'upsert', 'payload' => json_encode($built['event']),
            'filehash' => $built['file'] ? $built['file']->get_pathnamehash() : null,
            'sha256' => $built['sha256'], 'revision' => $built['revision'], 'fingerprint' => $built['fingerprint'],
            'status' => 'pending', 'attempts' => 0, 'nextattempt' => 0, 'lasterror' => null,
            'timecreated' => $when, 'timemodified' => $when,
        ]);
    }

    /** Queue a delete. */
    public static function delete(string $courseid, int $cmid, ?int $when = null): ?int {
        global $DB;
        if (!collector::enabled()) {
            return null;
        }
        $when = $when ?? time();
        $itemid = collector::item_id($cmid);
        self::drop_pending($itemid);
        $event = mapper::delete(mapper::event_id(), $courseid, $itemid, $when);
        return $DB->insert_record(self::TABLE, (object) [
            'eventid' => $event['event_id'], 'itemid' => $itemid, 'courseid' => $courseid, 'op' => 'delete',
            'payload' => json_encode($event), 'filehash' => null, 'sha256' => null, 'revision' => null,
            'fingerprint' => null, 'status' => 'pending', 'attempts' => 0, 'nextattempt' => 0,
            'lasterror' => null, 'timecreated' => $when, 'timemodified' => $when,
        ]);
    }

    /** Never-attempted queued rows for an item are superseded by a newer change. */
    protected static function drop_pending(string $itemid): void {
        global $DB;
        $DB->delete_records_select(self::TABLE, 'itemid = :i AND status = :s AND attempts = 0',
            ['i' => $itemid, 's' => 'pending']);
    }

    /** Rows due for sending, oldest first. */
    public static function due(int $limit, ?int $now = null): array {
        global $DB;
        return array_values($DB->get_records_select(self::TABLE, 'status = :s AND nextattempt <= :n',
            ['s' => 'pending', 'n' => $now ?? time()], 'id ASC', '*', 0, $limit));
    }

    /** NEXA gave a final answer for this event. */
    public static function settled(\stdClass $row, string $status, ?string $reason = null): void {
        global $DB;
        $final = $status === 'rejected' ? 'rejected' : 'sent';
        $DB->update_record(self::TABLE, (object) ['id' => $row->id, 'status' => $final,
            'lasterror' => $reason, 'timemodified' => time()]);
        $state = $row->op === 'delete' ? 'deleted' : ($final === 'rejected' ? 'rejected' : 'sent');
        self::note_item($row->itemid, $row->courseid, $row->revision, $row->fingerprint, $state, $reason);
    }

    /** Sending failed (network, NEXA down, auth): try again later. */
    public static function failed(\stdClass $row, string $error): void {
        global $DB;
        $attempts = (int) $row->attempts + 1;
        $DB->update_record(self::TABLE, (object) ['id' => $row->id, 'attempts' => $attempts,
            'nextattempt' => mapper::backoff($attempts, time()), 'lasterror' => substr($error, 0, 1000),
            'timemodified' => time()]);
    }

    /** What NEXA has been told about each item: drives the nightly diff. */
    public static function note_item(string $itemid, string $courseid, ?string $revision, ?string $fingerprint,
                                     string $state, ?string $detail): void {
        global $DB;
        $rec = $DB->get_record(self::ITEMS, ['itemid' => $itemid]);
        $data = ['itemid' => $itemid, 'courseid' => $courseid, 'state' => $state,
                 'detail' => $detail === null ? null : substr($detail, 0, 1000), 'timemodified' => time()];
        if ($revision !== null) {
            $data['revision'] = $revision;
        }
        if ($fingerprint !== null) {
            $data['fingerprint'] = $fingerprint;
        }
        if ($rec) {
            $data['id'] = $rec->id;
            $DB->update_record(self::ITEMS, (object) $data);
        } else {
            $DB->insert_record(self::ITEMS, (object) ($data + ['revision' => null, 'fingerprint' => null]));
        }
    }

    /** Housekeeping: settled rows older than 30 days are removed. */
    public static function prune(int $days = 30): void {
        global $DB;
        $DB->delete_records_select(self::TABLE, "status <> 'pending' AND timemodified < :t",
            ['t' => time() - $days * 86400]);
    }

    /** Counts for the admin status page and monitoring. */
    public static function stats(): array {
        global $DB;
        $out = [];
        foreach ($DB->get_records_sql("SELECT status, COUNT(1) AS n FROM {" . self::TABLE . "} GROUP BY status")
                 as $r) {
            $out[$r->status] = (int) $r->n;
        }
        $out['oldest_pending'] = $DB->get_field_sql("SELECT MIN(timecreated) FROM {" . self::TABLE . "}
            WHERE status = 'pending'");
        return $out;
    }
}
