<?php
// This file is part of the local_nexa_integration plugin.
//
// Reads what NEXA needs about one course activity from Moodle and turns it
// into a sync event with mapper. Supported activities: page, label, book,
// resource (PDF, DOCX, PPTX) and, since 2.0.1, presentationview (EduNex's
// lesson presentation activity, which stores its file the same way as
// resource). Private and user files are never read.

namespace local_nexa_integration\sync;

defined('MOODLE_INTERNAL') || die();

class collector {

    /** Activity types NEXA can learn from. */
    const KINDS = ['page', 'label', 'book', 'resource', 'presentationview'];

    /** File types NEXA extracts text from. */
    const FILE_TYPES = ['pdf', 'docx', 'pptx'];

    /** Is knowledge sync configured and switched on? */
    public static function enabled(): bool {
        $c = get_config('local_nexa_integration');
        return !empty($c->sync_enabled) && !empty($c->knowledge_url) && !empty($c->sync_token)
            && !empty($c->site_id);
    }

    public static function site(): array {
        $c = get_config('local_nexa_integration');
        $site = ['system' => 'moodle', 'site_id' => (string) $c->site_id];
        if (!empty($c->school_id)) {
            $site['school_id'] = (string) $c->school_id;
        }
        return $site;
    }

    public static function item_id(int $cmid): string {
        return 'cm' . $cmid;
    }

    /**
     * Facts about one activity and its course, or null if NEXA does not take
     * this kind of activity.
     *
     * @return array|null [course, cm_info, curriculum, visibility, approval, title, url]
     */
    public static function facts(int $courseid, int $cmid): ?array {
        $modinfo = get_fast_modinfo($courseid);
        try {
            $cm = $modinfo->get_cm($cmid);
        } catch (\moodle_exception $e) {
            return null;
        }
        if (!in_array($cm->modname, self::KINDS, true) || !empty($cm->deletioninprogress)) {
            return null;
        }
        $course = get_course($courseid);
        $c = get_config('local_nexa_integration');
        [$fsubject, $fgrade] = self::custom_fields($courseid);
        $tags = \core_tag_tag::get_item_tags_array('core', 'course_modules', $cm->id);
        $approvedcats = array_filter(array_map('intval', explode(',', (string) ($c->approved_categories ?? ''))));
        return [
            'course' => $course,
            'cm' => $cm,
            'curriculum' => mapper::curriculum(format_string($course->fullname), (string) $course->shortname,
                $fsubject, $fgrade) + ['language' => (string) ($c->language ?: 'en')],
            'visibility' => mapper::visibility(['coursevisible' => $course->visible, 'visible' => $cm->visible,
                'visibleoncoursepage' => $cm->visibleoncoursepage, 'availability' => $cm->availability]),
            'approval' => mapper::approval(array_values($tags), (string) ($c->approval_tag ?: 'nexa-approved'),
                (int) $course->category, $approvedcats),
            'title' => format_string($cm->name, true, ['context' => \context_module::instance($cm->id)]),
            'url' => (new \moodle_url('/mod/' . $cm->modname . '/view.php', ['id' => $cm->id]))->out(false),
        ];
    }

    /** Values of the optional course custom fields nexa_subject and nexa_grade. */
    protected static function custom_fields(int $courseid): array {
        $subject = $grade = null;
        try {
            $handler = \core_course\customfield\course_handler::create();
            foreach ($handler->get_instance_data($courseid, true) as $d) {
                $short = $d->get_field()->get('shortname');
                if ($short === 'nexa_subject') {
                    $subject = (string) $d->export_value();
                } else if ($short === 'nexa_grade') {
                    $grade = (string) $d->export_value();
                }
            }
        } catch (\Throwable $e) {
            // No custom fields on this site: fall back to the course name.
            debugging('local_nexa_integration: custom fields unavailable: ' . $e->getMessage(), DEBUG_DEVELOPER);
        }
        return [$subject, $grade];
    }

    /**
     * The activity's content and revision, without reading file bytes. For a
     * resource the revision uses Moodle's own content hash, so the nightly
     * scan never has to read every file to see what changed.
     *
     * @return array|null [revision, body|null, format, file stored_file|null]
     */
    public static function content(\cm_info $cm): ?array {
        global $DB;
        switch ($cm->modname) {
            case 'page':
                $p = $DB->get_record('page', ['id' => $cm->instance], 'id, content, timemodified', MUST_EXIST);
                return ['revision' => (string) $p->timemodified, 'body' => (string) $p->content, 'format' => 'html'];
            case 'label':
                $l = $DB->get_record('label', ['id' => $cm->instance], 'id, intro, timemodified', MUST_EXIST);
                return ['revision' => (string) $l->timemodified, 'body' => (string) $l->intro, 'format' => 'html'];
            case 'book':
                $chs = $DB->get_records('book_chapters', ['bookid' => $cm->instance, 'hidden' => 0], 'pagenum',
                    'id, title, content, timemodified');
                $body = '';
                $rev = 0;
                foreach ($chs as $ch) {
                    $body .= '<h2>' . s($ch->title) . '</h2>' . $ch->content;
                    $rev = max($rev, (int) $ch->timemodified);
                }
                return $body === '' ? null : ['revision' => (string) $rev, 'body' => $body, 'format' => 'html'];
            case 'resource':
            case 'presentationview':
                $fs = get_file_storage();
                $files = $fs->get_area_files(\context_module::instance($cm->id)->id, 'mod_' . $cm->modname, 'content', 0,
                    'sortorder DESC, id ASC', false);
                foreach ($files as $f) {
                    $ext = strtolower(pathinfo($f->get_filename(), PATHINFO_EXTENSION));
                    if (in_array($ext, self::FILE_TYPES, true)) {
                        return ['revision' => $f->get_timemodified() . '-' . substr($f->get_contenthash(), 0, 12),
                                'file' => $f, 'format' => null, 'body' => null];
                    }
                }
                return null;
        }
        return null;
    }

    /**
     * A change fingerprint over everything NEXA is told about an item, so the
     * nightly scan notices a visibility or approval change as well as an edit.
     */
    public static function fingerprint(array $facts, array $content): string {
        return sha1(json_encode([$content['revision'], $facts['visibility'], $facts['approval']['status'],
            $facts['title'], $facts['curriculum'], (string) get_config('local_nexa_integration', 'scope')]));
    }

    /**
     * Build the upsert event for an activity.
     *
     * @return array either ['event' => [...], 'file' => stored_file|null, 'fingerprint' => ..] or ['skip' => reason]
     */
    public static function build(int $courseid, int $cmid, int $occurred): array {
        $facts = self::facts($courseid, $cmid);
        if ($facts === null) {
            return ['skip' => 'not a supported activity'];
        }
        if (empty($facts['curriculum']['grade_levels'])) {
            return ['skip' => 'no grade: set the course custom field nexa_grade or put "Grade NN" in the course name'];
        }
        $content = self::content($facts['cm']);
        if ($content === null) {
            return ['skip' => 'no content NEXA can read (empty, or not PDF, DOCX or PPTX)'];
        }
        $file = $content['file'] ?? null;
        if ($file) {
            $tmp = $file->copy_content_to_temp('local_nexa_integration');
            $sha = hash_file('sha256', $tmp);
            @unlink($tmp);
            $payload = ['file' => ['sha256' => $sha, 'name' => $file->get_filename(),
                'mime' => $file->get_mimetype(), 'size' => (int) $file->get_filesize()]];
        } else {
            $payload = ['format' => 'html', 'body' => $content['body']];
        }
        $scope = get_config('local_nexa_integration', 'scope') === 'national' ? 'national' : 'school';
        $event = mapper::upsert([
            'event_id' => mapper::event_id(), 'occurred_at' => $occurred,
            'course_id' => (string) $courseid, 'course_name' => format_string($facts['course']->fullname),
            'item_id' => self::item_id($cmid), 'kind' => $facts['cm']->modname, 'revision' => $content['revision'],
            'title' => $facts['title'], 'url' => $facts['url'], 'visibility' => $facts['visibility'],
            'approval' => $facts['approval'], 'scope' => $scope, 'curriculum' => $facts['curriculum'],
            'content' => $payload,
        ]);
        return ['event' => $event, 'file' => $file, 'sha256' => $payload['file']['sha256'] ?? null,
                'revision' => $content['revision'], 'fingerprint' => self::fingerprint($facts, $content)];
    }
}
