<?php
// This file is part of the local_nexa_integration plugin.

namespace local_nexa_integration\task;

use local_nexa_integration\sync\client;
use local_nexa_integration\sync\client_exception;
use local_nexa_integration\sync\collector;
use local_nexa_integration\sync\outbox;
use local_nexa_integration\sync\mapper;

defined('MOODLE_INTERNAL') || die();

/**
 * Every few minutes: send due outbox rows to NEXA.
 *
 * For an upsert carrying a file, the file is uploaded first (by SHA-256) if
 * NEXA does not already have it. A row leaves the queue only when NEXA gives a
 * final answer for its event: accepted (NEXA owns it from here), duplicate,
 * stale, or rejected (logged with NEXA's reason; resending would not help).
 * Network errors and NEXA outages keep the rows queued with backoff.
 */
class push_outbox extends \core\task\scheduled_task {

    public function get_name() {
        return get_string('pushoutboxtask', 'local_nexa_integration');
    }

    public function execute() {
        if (!collector::enabled()) {
            mtrace('nexa sync: not configured, skipping.');
            return;
        }
        $limit = max(1, min(100, (int) get_config('local_nexa_integration', 'batch_size') ?: 50));
        $client = new client();
        $fs = get_file_storage();
        $sent = $failed = 0;
        for ($round = 0; $round < 20; $round++) {
            $rows = outbox::due($limit);
            if (!$rows) {
                break;
            }
            $ready = [];
            foreach ($rows as $row) {
                if ($row->op === 'upsert' && $row->sha256) {
                    try {
                        if (!$client->has_content($row->sha256)) {
                            $file = $row->filehash ? $fs->get_file_by_hash($row->filehash) : false;
                            if (!$file) {
                                // The file changed or went away since the change was queued.
                                outbox::settled($row, 'rejected', 'file no longer in Moodle; a newer change follows');
                                continue;
                            }
                            $tmp = $file->copy_content_to_temp('local_nexa_integration');
                            try {
                                if (hash_file('sha256', $tmp) !== $row->sha256) {
                                    outbox::settled($row, 'rejected', 'file changed since queued; a newer change follows');
                                    continue;
                                }
                                $client->put_content($row->sha256, $tmp);
                            } finally {
                                @unlink($tmp);
                            }
                        }
                    } catch (client_exception $e) {
                        outbox::failed($row, $e->getMessage());
                        $failed++;
                        continue;
                    }
                }
                $ready[] = $row;
            }
            if (!$ready) {
                continue;
            }
            try {
                $resp = $client->post_events(mapper::batch(collector::site(),
                    array_map(function($r) {
                        return json_decode($r->payload, true);
                    }, $ready)));
            } catch (client_exception $e) {
                if ($e->status === 422 && count($ready) > 1) {
                    // One malformed event must not block the rest: send singly.
                    foreach ($ready as $row) {
                        $this->send_one($client, $row, $sent, $failed);
                    }
                    continue;
                }
                foreach ($ready as $row) {
                    outbox::failed($row, $e->getMessage());
                    $failed++;
                }
                mtrace('nexa sync: ' . $e->getMessage());
                if ($e->status === 0 || $e->status >= 500 || in_array($e->status, [401, 403], true)) {
                    break; // NEXA down, offline or misconfigured: wait for the backoff
                }
                continue;
            }
            $this->settle($ready, $resp, $sent, $failed);
        }
        outbox::prune();
        mtrace("nexa sync: sent={$sent} failed={$failed}");
    }

    protected function send_one(client $client, \stdClass $row, int &$sent, int &$failed): void {
        try {
            $resp = $client->post_events(mapper::batch(collector::site(), [json_decode($row->payload, true)]));
            $this->settle([$row], $resp, $sent, $failed);
        } catch (client_exception $e) {
            if ($e->status === 422) {
                outbox::settled($row, 'rejected', $e->getMessage());
            } else {
                outbox::failed($row, $e->getMessage());
            }
            $failed++;
        }
    }

    protected function settle(array $rows, array $resp, int &$sent, int &$failed): void {
        $byid = [];
        foreach ($resp['results'] ?? [] as $r) {
            $byid[$r['event_id']] = $r;
        }
        foreach ($rows as $row) {
            $r = $byid[$row->eventid] ?? null;
            if ($r === null) {
                outbox::failed($row, 'no result for this event in the response');
                $failed++;
            } else if ($r['status'] === 'rejected') {
                outbox::settled($row, 'rejected', $r['reason'] ?? 'rejected');
                mtrace("nexa sync: REJECTED {$row->itemid}: " . ($r['reason'] ?? ''));
            } else {
                outbox::settled($row, $r['status']);
                $sent++;
            }
        }
    }
}
