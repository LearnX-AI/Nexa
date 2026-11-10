<?php
// Standalone check of the plugin's mapping rules, without Moodle.
//
//   php tests/contract_check.php > batches.json
//
// Exits non-zero if a rule is broken. Prints sync batches built by the mapper,
// which v1/tests/test_moodle_contract.py validates with NEXA's own contract
// model and pushes through the real sync endpoint.

define('MOODLE_INTERNAL', true);
require_once(__DIR__ . '/../classes/sync/mapper.php');

use local_nexa_integration\sync\mapper;

$fail = 0;
function check($label, $ok) {
    global $fail;
    if (!$ok) {
        fwrite(STDERR, "FAIL: $label\n");
        $fail++;
    }
}

// Visibility.
$v = function($cv, $vis, $vocp, $av) {
    return mapper::visibility(['coursevisible' => $cv, 'visible' => $vis, 'visibleoncoursepage' => $vocp,
        'availability' => $av]);
};
check('visible -> students', $v(1, 1, 1, null) === 'students');
check('empty availability tree -> students', $v(1, 1, 1, '{"op":"&","c":[],"showc":[]}') === 'students');
check('restricted -> teachers', $v(1, 1, 1, '{"op":"&","c":[{"type":"group","id":3}],"showc":[true]}') === 'teachers');
check('hidden -> hidden', $v(1, 0, 1, null) === 'hidden');
check('stealth -> hidden', $v(1, 1, 0, null) === 'hidden');
check('hidden course -> hidden', $v(0, 1, 1, null) === 'hidden');

// Approval.
check('tag approves', mapper::approval(['NEXA-Approved', 'x'], 'nexa-approved', 7, [])['status'] === 'approved');
check('category approves', mapper::approval([], 'nexa-approved', 7, ['3', '7'])['status'] === 'approved');
check('otherwise pending', mapper::approval(['other'], 'nexa-approved', 7, [3])['status'] === 'pending_review');

// Curriculum.
$c = mapper::curriculum('Grade 12 Biology', 'BIO12', null, null);
check('subject from name', $c['subject'] === 'Biology');
check('grade from name', $c['grade_levels'] === [12]);
$c = mapper::curriculum('Year 11-12 General Science', 'SCI', null, null);
check('general science beats science', $c['subject'] === 'General Science');
$c = mapper::curriculum('Physics', 'PHY', 'Physics', '11, 12');
check('custom fields', $c['subject'] === 'Physics' && $c['grade_levels'] === [11, 12]);
check('no grade', mapper::curriculum('Staff room', 'STAFF', null, null)['grade_levels'] === []);
check('accounting -> General', mapper::curriculum('Grade 11 Accounting', 'ACC11', null, null)['subject'] === 'General');

// Backoff: 1 min, doubling, capped at 6 h.
check('backoff 1', mapper::backoff(1, 0) === 60);
check('backoff 4', mapper::backoff(4, 0) === 480);
check('backoff cap', mapper::backoff(30, 0) === 6 * 3600);
check('event ids unique', mapper::event_id() !== mapper::event_id());

$site = ['system' => 'moodle', 'site_id' => 'edunex-ncd', 'school_id' => 'pg-ncd-0042'];
$t = 1793520000; // 2026-11-01 08:00 UTC
$page = mapper::upsert(['event_id' => mapper::event_id(), 'occurred_at' => $t, 'course_id' => '17',
    'course_name' => 'Grade 12 Biology', 'item_id' => 'cm345', 'kind' => 'page', 'revision' => '1793519000',
    'title' => 'Photosynthesis', 'url' => 'https://moodle.example/mod/page/view.php?id=345',
    'visibility' => 'students', 'approval' => mapper::approval(['nexa-approved'], 'nexa-approved', 1, []),
    'scope' => 'school', 'curriculum' => mapper::curriculum('Grade 12 Biology', 'BIO12', null, null) + ['language' => 'en'],
    'content' => ['format' => 'html', 'body' => '<h2>Photosynthesis</h2><p>Chloroplasts capture light energy and '
        . 'make glucose from carbon dioxide and water.</p>']]);
$hidden = mapper::upsert(['event_id' => mapper::event_id(), 'occurred_at' => $t + 60, 'course_id' => '17',
    'item_id' => 'cm346', 'kind' => 'label', 'revision' => '1793519100', 'title' => 'Answer key for quiz 3',
    'visibility' => $v(1, 0, 1, null), 'approval' => mapper::approval([], 'nexa-approved', 5, [5]),
    'scope' => 'school', 'curriculum' => mapper::curriculum('Grade 12 Biology', 'BIO12', null, null),
    'content' => ['format' => 'html', 'body' => '<p>Answer key: question 3 is the chloroplast.</p>']]);
$del = mapper::delete(mapper::event_id(), '17', 'cm300', $t + 120);
check('upsert shape', $page['op'] === 'upsert' && $page['occurred_at'] === '2026-11-01T08:00:00Z'
    && !isset($page['item']['owner_ref']));
check('delete shape', $del['item'] === ['course_id' => '17', 'item_id' => 'cm300']);

echo json_encode([mapper::batch($site, [$page, $hidden, $del])], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
exit($fail ? 1 : 0);
