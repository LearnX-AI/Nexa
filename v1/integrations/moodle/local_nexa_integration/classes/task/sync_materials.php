<?php
namespace local_nexa_integration\task;

defined('MOODLE_INTERNAL') || die();

/**
 * Scheduled task: walk every course, find uploaded pdf/docx/pptx files, and
 * push new or changed ones to the Nexa /ingest-material endpoint so Nexa can
 * answer from them. A manifest (stored in plugin config) records what has
 * already been sent, so each run only ships what changed.
 */
class sync_materials extends \core\task\scheduled_task {

    /** Extensions Nexa can extract text from. */
    const ALLOWED = ['pdf', 'docx', 'pptx'];

    public function get_name() {
        return get_string('syncmaterialstask', 'local_nexa_integration');
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

        // Load the manifest of already-sent files: {contenthash => timemodified}.
        $manifest = json_decode(
            (string) get_config('local_nexa_integration', 'sync_manifest'), true);
        if (!is_array($manifest)) {
            $manifest = [];
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
                $context = \context_module::instance($cm->id);
                $files = $fs->get_area_files(
                    $context->id, 'mod_resource', 'content', 0, 'sortorder', false);

                foreach ($files as $file) {
                    $name = $file->get_filename();
                    $ext = strtolower(pathinfo($name, PATHINFO_EXTENSION));
                    if (!in_array($ext, self::ALLOWED, true)) {
                        continue;
                    }
                    $hash = $file->get_contenthash();
                    $mtime = (int) $file->get_timemodified();
                    // Already sent and unchanged -> skip.
                    if (isset($manifest[$hash]) && $manifest[$hash] >= $mtime) {
                        $skipped++;
                        continue;
                    }

                    $ok = $this->send_file($endpoint, $token, $file, $coursename, $subject, $grade);
                    if ($ok) {
                        $manifest[$hash] = $mtime;
                        $sent++;
                        mtrace("nexa sync: sent {$name} ({$subject} {$grade}) from {$coursename}");
                        // Persist after each success so a mid-run failure doesn't resend.
                        set_config('sync_manifest', json_encode($manifest), 'local_nexa_integration');
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
