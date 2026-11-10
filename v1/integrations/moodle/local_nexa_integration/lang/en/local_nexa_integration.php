<?php
defined('MOODLE_INTERNAL') || die();

$string['pluginname']    = 'Nexa Chatbot Integration';
$string['welcome']       = 'Welcome, {$a}! Nexa is ready to help you.';
$string['nexa_base_url'] = 'Nexa Chat Base URL';
$string['nexa_base_url_desc'] = 'Base URL of the Nexa chat frontend, e.g. https://nexa.example.com';
$string['show_block']    = 'Show Nexa Chat launcher';
$string['show_block_desc'] = 'Injects the Nexa launcher into every page for logged-in users.';
$string['bubble_size']   = 'Collapsed launcher size (px)';
$string['bubble_size_desc'] = 'Width and height of the iframe while Nexa is closed. Keep this just big enough for the launcher bubble - any extra area sits on top of the page and blocks clicks underneath it.';
$string['edge_offset']   = 'Edge offset (px)';
$string['edge_offset_desc'] = 'Gap between the launcher and the bottom-right corner of the viewport.';
$string['welcome_notice'] = 'Show welcome notification on login';
$string['welcome_notice_desc'] = 'Displays a "Welcome, name" notification each time a user logs in.';
$string['notifytask']    = 'Notify Nexa backend of user login';
$string['syncmaterialstask'] = 'Sync course materials to Nexa';
$string['ingest_token']  = 'Nexa ingest token';
$string['ingest_token_desc'] = 'Shared secret that authenticates material uploads to the Nexa /ingest-material endpoint. Must match NEXA_INGEST_TOKEN set on the Nexa server.';
$string['privacy:metadata'] = 'The Nexa Chatbot Integration plugin does not store personal data itself, but transmits the user email address and full name to the configured external Nexa service.';
