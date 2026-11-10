<?php
namespace local_nexa_integration\task;

defined('MOODLE_INTERNAL') || die();

/**
 * LEGACY (v0 chat only). Disabled by default since 2.0.
 *
 * Pushes course files to the v0 chat's /ingest-material endpoint, which has no
 * approval, audience or deletion handling. Knowledge sync to NEXA v1 (the
 * push_outbox and sync_materials tasks) replaces it. Keep it enabled only
 * while the v0 chat still answers from LMS files.
 *
 * Changed in 2.0 for safety: only activities students can see are sent
 * (hidden, stealth and access-restricted files used to be sent too, which
 * made answer keys answerable), and the sent-file manifest is keyed by course
 * as well as content, so the same file in two courses reaches both.
 */
class legacy_v0_push extends \core\task\scheduled_task {

    /** Extensions Nexa can extract text from. */
    const ALLOWED = ['pdf', 'docx', 'pptx'];

    public function get_name() {
        return get_string('legacyv0task', 'local_nexa_integration');
    }

    public function execute() {
        global $CFG;
        require_once($CFG->libdir . '/filelib.php');

        $base = trim((string) get_config('local_nexa_integration', 'nexa_base_url'));
        if ($base === '') {
            mtrace('nexa sync: no base URL configured, skipping.');
            return;
        }
        $token = trim((string) get_config('local_nexa_integration', 'ingest_token'));
        if ($token === '') {
            mtrace('nexa sync: no ingest token configured, skipping.');
            return;
        }
        $endpoint = rtrim($base, '/') . '/ingest-material';

        // Manifest of already-sent files: {courseid:contenthash => timemodified}.
        $manifest = json_decode(
            (string) get_config('local_nexa_integration', 'legacy_manifest'), true);
        if (!is_array($manifest)) {
            $manifest = [];
        }
        // The 1.x manifest was keyed by content only. Files it records were
        // already sent to v0, which does not de-duplicate, so they are not sent
        // again.
        $old = json_decode((string) get_config('local_nexa_integration', 'sync_manifest'), true);
        if (!is_array($old)) {
            $old = [];
        }

        $fs = get_file_storage();
        $courses = get_courses();
        $sent = 0;
        $skipped = 0;

        foreach ($courses as $course) {
            if ($course->id == SITEID) {
                continue;
            }
            $coursename = format_string($course->fullname);
            list($subject, $grade) = $this->subject_grade($coursename, $course->shortname ?? '');

            try {
                $modinfo = get_fast_modinfo($course->id);
            } catch (\Throwable $e) {
                mtrace("nexa sync: modinfo failed for course {$course->id}: " . $e->getMessage());
                continue;
            }

            foreach ($modinfo->get_cms() as $cm) {
                // 'resource' = a file a teacher uploaded. Extend here for others.
                if ($cm->modname !== 'resource') {
                    continue;
                }
                $vis = \local_nexa_integration\sync\mapper::visibility(['coursevisible' => $course->visible,
                    'visible' => $cm->visible, 'visibleoncoursepage' => $cm->visibleoncoursepage,
                    'availability' => $cm->availability]);
                if ($vis !== 'students') {
                    continue; // never send what students cannot see
                }
                $context = \context_module::instance($cm->id);
                $files = $fs->get_area_files(
                    $context->id, 'mod_resource', 'content', 0, 'sortorder', false);

                foreach ($files as $file) {
                    $name = $file->get_filename();
                    $ext = strtolower(pathinfo($name, PATHINFO_EXTENSION));
                    if (!in_array($ext, self::ALLOWED, true)) {
                        continue;
                    }
                    $hash = $course->id . ':' . $file->get_contenthash();
                    $mtime = (int) $file->get_timemodified();
                    // Already sent and unchanged -> skip.
                    $oldhash = $file->get_contenthash();
                    if ((isset($manifest[$hash]) && $manifest[$hash] >= $mtime)
                            || (isset($old[$oldhash]) && $old[$oldhash] >= $mtime)) {
                        $skipped++;
                        continue;
                    }

                    $ok = $this->send_file($endpoint, $token, $file, $coursename, $subject, $grade);
                    if ($ok) {
                        $manifest[$hash] = $mtime;
                        $sent++;
                        mtrace("nexa sync: sent {$name} ({$subject} {$grade}) from {$coursename}");
                        // Persist after each success so a mid-run failure doesn't resend.
                        set_config('legacy_manifest', json_encode($manifest), 'local_nexa_integration');
                    } else {
                        mtrace("nexa sync: FAILED {$name} from {$coursename}");
                    }
                }
            }
        }

        mtrace("nexa sync: done. sent={$sent}, skipped(existing)={$skipped}");
    }

    /**
     * Derive subject and grade from the course name/shortname.
     */
    protected function subject_grade(string $name, string $short): array {
        $hay = strtolower($name . ' ' . $short);
        $subject = '';
        $map = [
            'biology' => 'Biology', 'chemistry' => 'Chemistry', 'physics' => 'Physics',
            'mathematics' => 'Mathematics', 'maths' => 'Mathematics', 'math' => 'Mathematics',
            'english' => 'English', 'accounting' => 'Accounting', 'economics' => 'Economics',
            'geology' => 'Geology', 'agriculture' => 'Agriculture',
            'business' => 'Business Studies', 'ict' => 'ICT',
        ];
        foreach ($map as $key => $val) {
            if (strpos($hay, $key) !== false) {
                $subject = $val;
                break;
            }
        }
        $grade = '';
        if (preg_match('/grade\s*(\d{1,2})/', $hay, $m)) {
            $grade = $m[1];
        }
        return [$subject, $grade];
    }

    /**
     * POST one file to Nexa's ingest endpoint. Returns true on HTTP 200.
     */
    protected function send_file($endpoint, $token, \stored_file $file, $course, $subject, $grade): bool {
        // Write the file to a temp path so curl can multipart-upload it.
        $tmp = make_request_directory() . '/' . $file->get_filename();
        try {
            $file->copy_content_to($tmp);
        } catch (\Throwable $e) {
            mtrace('nexa sync: copy failed - ' . $e->getMessage());
            return false;
        }

        $curl = new \curl();
        $curl->setopt([
            'CURLOPT_TIMEOUT'        => 120,
            'CURLOPT_CONNECTTIMEOUT' => 15,
            'CURLOPT_FOLLOWLOCATION' => 0,
        ]);
        $params = [
            'file'    => new \CURLFile($tmp, '', $file->get_filename()),
            'course'  => $course,
            'subject' => $subject,
            'grade'   => $grade,
            'token'   => $token,
        ];
        $response = $curl->post($endpoint, $params);
        $info = $curl->get_info();
        $code = (int) ($info['http_code'] ?? 0);

        @unlink($tmp);

        if (!empty($curl->error) || $code !== 200) {
            mtrace('nexa sync: HTTP ' . $code . ' ' . ($curl->error ?: substr((string) $response, 0, 200)));
            return false;
        }
        return true;
    }
}
