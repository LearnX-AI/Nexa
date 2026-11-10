<?php
// This file is part of the local_nexa_integration plugin.
//
// HTTP client for the NEXA Knowledge Service sync API (nexa.lms-sync/1.0).
// The sync token travels in the Authorization header, never in a URL or a
// form field, and is never logged.

namespace local_nexa_integration\sync;

defined('MOODLE_INTERNAL') || die();

require_once($GLOBALS['CFG']->libdir . '/filelib.php');

class client_exception extends \moodle_exception {
    /** @var int HTTP status, 0 for a network error */
    public $status;

    public function __construct(int $status, string $message) {
        $this->status = $status;
        parent::__construct('generalexceptionmessage', 'error', '', $message);
    }
}

class client {

    /** @var string */
    protected $base;
    /** @var string */
    protected $token;

    public function __construct(?string $base = null, ?string $token = null) {
        $this->base = rtrim($base ?? (string) get_config('local_nexa_integration', 'knowledge_url'), '/');
        $this->token = $token ?? (string) get_config('local_nexa_integration', 'sync_token');
    }

    protected function curl(int $timeout = 60): \curl {
        $c = new \curl(['ignoresecurity' => false]);
        $c->setopt(['CURLOPT_TIMEOUT' => $timeout, 'CURLOPT_CONNECTTIMEOUT' => 15, 'CURLOPT_FOLLOWLOCATION' => 0]);
        $c->setHeader(['Authorization: Bearer ' . $this->token]);
        return $c;
    }

    protected function check(\curl $c, $body): array {
        $code = (int) ($c->get_info()['http_code'] ?? 0);
        if (!empty($c->error) || $code === 0) {
            throw new client_exception(0, 'NEXA unreachable: ' . $c->error);
        }
        if ($code >= 300) {
            throw new client_exception($code, 'NEXA HTTP ' . $code . ': ' . substr((string) $body, 0, 300));
        }
        $data = json_decode((string) $body, true);
        return is_array($data) ? $data : [];
    }

    /** Does NEXA already hold this file? */
    public function has_content(string $sha256): bool {
        $c = $this->curl(30);
        $c->head($this->base . '/v1/knowledge/lms/content/' . $sha256);
        $code = (int) ($c->get_info()['http_code'] ?? 0);
        if ($code === 200) {
            return true;
        }
        if ($code === 404) {
            return false;
        }
        throw new client_exception($code, 'NEXA HEAD content: HTTP ' . $code . ' ' . $c->error);
    }

    /** Upload a file NEXA will reference by SHA-256. */
    public function put_content(string $sha256, string $path): array {
        $c = $this->curl(300);
        $c->setHeader(['Content-Type: application/octet-stream']);
        $body = $c->put($this->base . '/v1/knowledge/lms/content/' . $sha256, ['file' => $path]);
        return $this->check($c, $body);
    }

    /** Send a batch of events; returns the per-event results. */
    public function post_events(array $batch): array {
        $c = $this->curl(300);
        $c->setHeader(['Content-Type: application/json']);
        $body = $c->post($this->base . '/v1/knowledge/lms/events', json_encode($batch));
        return $this->check($c, $body);
    }

    /** Full list of a course's items; NEXA reports differences and, with apply, withdraws extras. */
    public function reconcile(array $site, string $courseid, array $items, bool $apply): array {
        $c = $this->curl(300);
        $c->setHeader(['Content-Type: application/json']);
        $body = $c->post($this->base . '/v1/knowledge/lms/reconcile', json_encode([
            'contract' => mapper::CONTRACT, 'site' => $site, 'course_id' => $courseid,
            'items' => array_values($items), 'apply' => $apply]));
        return $this->check($c, $body);
    }

    /** NEXA's view of this site's sync, for the admin check. */
    public function status(): array {
        $c = $this->curl(30);
        $body = $c->get($this->base . '/v1/knowledge/lms/status');
        return $this->check($c, $body);
    }
}
