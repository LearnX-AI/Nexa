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
$string['syncmaterialstask'] = 'NEXA knowledge sync: nightly scan and reconcile';
$string['ingest_token']  = 'Legacy v0 ingest token';
$string['ingest_token_desc'] = 'Only for the legacy v0 push task (disabled by default): shared secret for the v0 chat\'s /ingest-material endpoint. Must match NEXA_INGEST_TOKEN on the v0 server. Not used by knowledge sync.';


// Knowledge sync (NEXA-M2-007).
$string['pushoutboxtask'] = 'NEXA knowledge sync: send queued changes';
$string['legacyv0task'] = 'LEGACY: push course files to the v0 Nexa chat';
$string['syncheading'] = 'Knowledge sync to NEXA';
$string['syncheading_desc'] = 'Sends approved course content (pages, labels, books, and PDF, DOCX and PPTX files) to the NEXA Knowledge Service so Nexa can answer from it, with citations. Only activities with the approval tag, or in an approved category, are served to learners. Hidden or restricted activities are served to teachers only. Changes are queued and survive network outages.';
$string['sync_enabled'] = 'Enable knowledge sync';
$string['sync_enabled_desc'] = 'Queue course content changes and send them to NEXA.';
$string['knowledge_url'] = 'NEXA Knowledge Service URL';
$string['knowledge_url_desc'] = 'Base URL of the NEXA Knowledge Service, for example https://nexa.example.org/knowledge. If it is on a private network address, allow it in Site administration > General > Security > HTTP security (cURL blocked hosts and allowed ports).';
$string['sync_token'] = 'NEXA sync token';
$string['sync_token_desc'] = 'Sync token issued by NEXA for this site (role lms_sync, bound to the site ID and school below). Sent only in the Authorization header.';
$string['site_id'] = 'Site ID';
$string['site_id_desc'] = 'This Moodle site\'s ID in NEXA, lowercase, for example edunex-ncd. Must match the token.';
$string['school_id'] = 'School ID';
$string['school_id_desc'] = 'The school this site serves, as in the token. School-scoped content is visible only to that school\'s users in NEXA.';
$string['scope'] = 'Content scope';
$string['scope_desc'] = 'School: content is visible only to this school\'s users. National: visible to every school; needs a national sync token.';
$string['scope_school'] = 'School';
$string['scope_national'] = 'National';
$string['approval_tag'] = 'Approval tag';
$string['approval_tag_desc'] = 'An activity carrying this tag is approved for NEXA. Untagged activities are sent as pending review and are not served.';
$string['approved_categories'] = 'Auto-approved categories';
$string['approved_categories_desc'] = 'Every supported activity in courses directly in these categories is approved for NEXA without a tag.';
$string['language'] = 'Content language';
$string['language_desc'] = 'Language of course content, as a BCP-47 tag: en, tpi (Tok Pisin), ho (Hiri Motu).';
$string['batch_size'] = 'Events per request';
$string['batch_size_desc'] = 'How many queued changes are sent in one request (1 to 100).';

// Privacy.
$string['privacy:metadata:nexa_chat'] = 'The chat launcher sends the logged-in user\'s email address and full name to the Nexa chat service.';
$string['privacy:metadata:nexa_chat:email'] = 'The user\'s email address.';
$string['privacy:metadata:nexa_chat:fullname'] = 'The user\'s full name.';
$string['privacy:metadata:nexa_knowledge'] = 'Knowledge sync sends course content (titles, text and files of course activities) to the NEXA Knowledge Service. It sends no user data.';
$string['privacy:metadata:nexa_knowledge:content'] = 'Course activity content.';
