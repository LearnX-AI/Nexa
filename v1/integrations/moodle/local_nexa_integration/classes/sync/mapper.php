<?php
// This file is part of the local_nexa_integration plugin.
//
// Pure mapping from Moodle facts to NEXA sync events (contract nexa.lms-sync/1.0).
// No Moodle API calls here, so the rules can be tested outside Moodle and
// checked against NEXA's own validator (see tests/contract_check.php).

namespace local_nexa_integration\sync;

defined('MOODLE_INTERNAL') || die();

class mapper {

    /** Contract this plugin speaks. */
    const CONTRACT = 'nexa.lms-sync/1.0';

    /** NEXA subjects (NEXA-M2-001); anything else is sent as General. */
    const SUBJECTS = [
        'biology' => 'Biology', 'chemistry' => 'Chemistry', 'physics' => 'Physics',
        'mathematics' => 'Mathematics', 'maths' => 'Mathematics', 'math' => 'Mathematics',
        'english' => 'English', 'engineering' => 'Engineering', 'technology' => 'Technology',
        'ict' => 'Technology', 'general science' => 'General Science', 'science' => 'General Science',
        'social science' => 'Social Science', 'social studies' => 'Social Science',
    ];

    /**
     * Who may see an activity, from Moodle's point of view.
     *
     *  students  visible on the course page, no access restrictions, course visible
     *  teachers  visible but restricted (availability conditions): sent as
     *            teacher-only, because NEXA cannot evaluate Moodle's conditions
     *  hidden    hidden, stealth, or in a hidden course: teacher-only
     *
     * @param array $f visible, visibleoncoursepage, availability, coursevisible
     * @return string
     */
    public static function visibility(array $f): string {
        if (empty($f['coursevisible']) || empty($f['visible']) || (isset($f['visibleoncoursepage'])
                && empty($f['visibleoncoursepage']))) {
            return 'hidden';
        }
        if (!empty($f['availability']) && trim((string) $f['availability']) !== '{"op":"&","c":[],"showc":[]}') {
            return 'teachers';
        }
        return 'students';
    }

    /**
     * Approval: an activity is approved for NEXA when it carries the approval
     * tag, or its course is in an auto-approved category.
     */
    public static function approval(array $tags, string $approvaltag, int $categoryid, array $approvedcats,
                                    ?string $by = null): array {
        $tagged = in_array(self::lower($approvaltag), array_map([self::class, 'lower'], $tags), true);
        $incat = in_array($categoryid, array_map('intval', $approvedcats), true);
        if ($tagged || $incat) {
            return ['status' => 'approved', 'by' => $by ?? ($tagged ? 'moodle-policy:tag' : 'moodle-policy:category')];
        }
        return ['status' => 'pending_review'];
    }

    /**
     * Subject and grades from course custom fields, else from the course name.
     *
     * @return array{subject: string, grade_levels: int[]}
     */
    public static function curriculum(string $fullname, string $shortname, ?string $fieldsubject,
                                      ?string $fieldgrade): array {
        $subject = 'General';
        $hay = self::lower(trim(($fieldsubject ?: '') !== '' ? $fieldsubject : $fullname . ' ' . $shortname));
        // Longest key first, so "general science" wins over "science".
        $keys = array_keys(self::SUBJECTS);
        usort($keys, function($a, $b) {
            return strlen($b) - strlen($a);
        });
        foreach ($keys as $k) {
            if (preg_match('/\b' . preg_quote($k, '/') . '\b/', $hay)) {
                $subject = self::SUBJECTS[$k];
                break;
            }
        }
        $grades = [];
        $src = ($fieldgrade ?? '') !== '' ? (string) $fieldgrade : $fullname . ' ' . $shortname;
        if (($fieldgrade ?? '') !== '' && preg_match_all('/\d{1,2}/', $src, $m)) {
            $grades = $m[0];
        } else if (preg_match_all('/(?:grade|gr\.?|year)\s*(\d{1,2})/i', $src, $m)) {
            $grades = $m[1];
        }
        $grades = array_values(array_unique(array_filter(array_map('intval', $grades), function($g) {
            return $g >= 1 && $g <= 13;
        })));
        sort($grades);
        return ['subject' => $subject, 'grade_levels' => $grades];
    }

    /** core_text::strtolower inside Moodle, mb_strtolower in the standalone check. */
    public static function lower($s): string {
        return class_exists('\\core_text') ? \core_text::strtolower((string) $s) : mb_strtolower((string) $s);
    }

    /** RFC 3339 UTC time. */
    public static function iso(int $ts): string {
        return gmdate('Y-m-d\TH:i:s\Z', $ts);
    }

    /** A fresh event id; stored with the outbox row and reused on every resend. */
    public static function event_id(): string {
        $b = random_bytes(16);
        return 'mdl-' . bin2hex($b);
    }

    /**
     * Build an upsert event.
     *
     * @param array $i keys: course_id, course_name, item_id, kind, revision, title, url, owner_ref,
     *                 visibility, approval, scope, curriculum, content, occurred_at, event_id
     */
    public static function upsert(array $i): array {
        $item = [
            'course_id' => (string) $i['course_id'],
            'item_id' => (string) $i['item_id'],
            'course_name' => $i['course_name'] ?? null,
            'kind' => $i['kind'] ?? null,
            'revision' => (string) $i['revision'],
            'title' => (string) $i['title'],
            'url' => $i['url'] ?? null,
            'owner_ref' => $i['owner_ref'] ?? null,
            'visibility' => $i['visibility'],
            'approval' => $i['approval'],
            'scope' => $i['scope'] ?? 'school',
            'curriculum' => $i['curriculum'],
            'content' => $i['content'],
        ];
        if (!empty($i['supersedes'])) {
            $item['supersedes'] = (string) $i['supersedes'];
        }
        return ['event_id' => $i['event_id'], 'op' => 'upsert', 'occurred_at' => self::iso((int) $i['occurred_at']),
                'item' => array_filter($item, function($v) {
                    return $v !== null;
                })];
    }

    public static function delete(string $eventid, string $courseid, string $itemid, int $occurred): array {
        return ['event_id' => $eventid, 'op' => 'delete', 'occurred_at' => self::iso($occurred),
                'item' => ['course_id' => $courseid, 'item_id' => $itemid]];
    }

    public static function batch(array $site, array $events): array {
        return ['contract' => self::CONTRACT, 'site' => $site, 'events' => array_values($events)];
    }

    /**
     * Next attempt after a failed send: exponential, 1 minute doubling to 6 hours.
     */
    public static function backoff(int $attempts, int $now): int {
        return $now + min(60 * (2 ** max(0, $attempts - 1)), 6 * 3600);
    }
}
