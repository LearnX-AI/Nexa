<?php
namespace local_nexa_integration\task;

defined('MOODLE_INTERNAL') || die();

/**
 * Fire-and-forget notification to the Nexa backend that a user has logged in.
 */
class notify_nexa extends \core\task\adhoc_task {

    /**
     * @return string
     */
    public function get_name() {
        return get_string('notifytask', 'local_nexa_integration');
    }

    public function execute() {
        $data = (array) $this->get_custom_data();

        if (empty($data['email'])) {
            return;
        }

        $base = trim((string) get_config('local_nexa_integration', 'nexa_base_url'));
        if ($base === '') {
            return;
        }

        $url = rtrim($base, '/')
            . '/?email=' . rawurlencode($data['email'])
            . '&name=' . rawurlencode($data['name'] ?? '');

        $curl = new \curl();
        $curl->setopt([
            'CURLOPT_TIMEOUT'        => 5,
            'CURLOPT_CONNECTTIMEOUT' => 3,
            'CURLOPT_FOLLOWLOCATION' => 0,
        ]);
        $curl->get($url);

        $info = $curl->get_info();
        if (!empty($curl->error)) {
            mtrace('local_nexa_integration: notify failed - ' . $curl->error);
        } else {
            mtrace('local_nexa_integration: notify HTTP ' . ($info['http_code'] ?? '?'));
        }
    }
}
