<?php
// This file is part of the local_nexa_integration plugin.
//
// Run inside Moodle: vendor/bin/phpunit --testsuite local_nexa_integration_testsuite

namespace local_nexa_integration;

use local_nexa_integration\sync\mapper;

defined('MOODLE_INTERNAL') || die();

/**
 * @covers \local_nexa_integration\sync\mapper
 */
final class mapper_test extends \basic_testcase {

    public function test_visibility(): void {
        $f = ['coursevisible' => 1, 'visible' => 1, 'visibleoncoursepage' => 1, 'availability' => null];
        $this->assertSame('students', mapper::visibility($f));
        $this->assertSame('hidden', mapper::visibility(['visible' => 0] + $f));
        $this->assertSame('hidden', mapper::visibility(['visibleoncoursepage' => 0] + $f));
        $this->assertSame('hidden', mapper::visibility(['coursevisible' => 0] + $f));
        $this->assertSame('teachers', mapper::visibility(
            ['availability' => '{"op":"&","c":[{"type":"group","id":3}],"showc":[true]}'] + $f));
    }

    public function test_approval_and_curriculum(): void {
        $this->assertSame('approved', mapper::approval(['NEXA-approved'], 'nexa-approved', 1, [])['status']);
        $this->assertSame('approved', mapper::approval([], 'nexa-approved', 4, [4])['status']);
        $this->assertSame('pending_review', mapper::approval([], 'nexa-approved', 4, [5])['status']);
        $c = mapper::curriculum('Grade 11 Chemistry', 'CHEM11', null, null);
        $this->assertSame(['subject' => 'Chemistry', 'grade_levels' => [11]], $c);
    }

    public function test_events(): void {
        $d = mapper::delete('mdl-1', '5', 'cm9', 0);
        $this->assertSame(['course_id' => '5', 'item_id' => 'cm9'], $d['item']);
        $this->assertSame('1970-01-01T00:00:00Z', $d['occurred_at']);
        $this->assertSame(mapper::CONTRACT, mapper::batch(['system' => 'moodle', 'site_id' => 's1'], [$d])['contract']);
    }
}
