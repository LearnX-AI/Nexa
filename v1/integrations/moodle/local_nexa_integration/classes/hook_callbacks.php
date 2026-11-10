<?php
namespace local_nexa_integration;

defined('MOODLE_INTERNAL') || die();

/**
 * Hook callbacks for Moodle 4.4 and later.
 */
class hook_callbacks {

    /**
     * Injects the Nexa launcher just before the page footer.
     *
     * @param \core\hook\output\before_footer_html_generation $hook
     */
    public static function before_footer_html_generation(
        \core\hook\output\before_footer_html_generation $hook
    ): void {
        $html = helper::get_launcher_html();
        if ($html !== '') {
            $hook->add_html($html);
        }
    }
}
